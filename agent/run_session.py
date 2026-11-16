"""Session runner: one supervised session of the full loop, inside the sandbox.

This file owns only the order of calls and the session lifecycle. The parts are injected:
  planner     iterable of (seed, mutation_chain)        Jiseon  (agent/planner.py)
  build       (seed, chain, attempt_id) -> TargetRequest Jiseon  (agent/generator.py)
  code_checks (seed, target_text) -> CodeChecks          Soona   (stub until delivered)
  verifier    VerifierInput -> VerifierOutput            Soona   (stub until delivered)
Target client, Logger and Budget are wired here.

Per attempt: kill/budget check -> build request -> send (Target client) -> verify -> log verdict.
The session stops on: planner exhausted, budget limit or kill switch (Budget records
budget_stop / kill), or any log write failure (CLAUDE.md §4). One session = one new log file.

Usage (inside the sandbox; sends real requests to the staging target, so a human confirms first):
  docker compose exec agent python -m agent.run_session --max-attempts 3
"""
from __future__ import annotations

import argparse
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import httpx

from agent import budget as budget_mod
from agent import target_client as tc
from agent.budget import Budget, BudgetStop
from agent.generator import build_request
from agent.logger import Logger, LogWriteError
from schemas.target_io import TargetRequest
from schemas.verifier_input import CodeChecks, Goal, VerifierInput, VerifierOutput

Planner = Iterable[tuple[dict, list[str]]]
Build = Callable[[dict, list[str], str], TargetRequest]
CodeCheck = Callable[[dict, str], CodeChecks]
Verifier = Callable[[VerifierInput], VerifierOutput]


def no_code_checks(seed: dict, target_text: str) -> CodeChecks:
    """Stand-in until Soona's code_checks (canary_found, refusal_matched) is delivered."""
    return CodeChecks()


def stub_verifier(vin: VerifierInput) -> VerifierOutput:
    """Stand-in until Soona's Verifier is delivered. Never claims a finding."""
    return VerifierOutput(
        attempt_id=vin.attempt_id, verdict="uncertain", basis="code", rationale="verifier not wired yet"
    )


@dataclass
class SessionResult:
    log_path: Path
    stop_reason: str
    attempts: int = 0
    sent: int = 0
    errors: Counter = field(default_factory=Counter)
    verdicts: Counter = field(default_factory=Counter)


def new_log_path(log_dir: Path) -> Path:
    return log_dir / f"session-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}.jsonl"


def run_session(
    planner: Planner,
    *,
    build: Build = build_request,
    code_checks: CodeCheck = no_code_checks,
    verifier: Verifier = stub_verifier,
    log_dir: Path = Path("logs"),
    kill_file: Path = budget_mod.KILL_FILE,
    transport: Optional[httpx.BaseTransport] = None,
    session_meta: Optional[dict] = None,
) -> SessionResult:
    log = Logger(new_log_path(log_dir))
    log.start_session(data={"runner": "agent.run_session", **(session_meta or {})})
    budget = Budget(log_event=log.log_event, kill_file=kill_file)
    client = tc.TargetClient(tc.budget_check_from(budget), log.log_event, transport=transport)
    sent_before = tc.CLIENT_COUNTER
    result = SessionResult(log_path=log.path, stop_reason="planner_exhausted")

    try:
        for i, (seed, chain) in enumerate(planner):
            budget.check()  # kill switch / time / cost before doing any work for this attempt
            req = build(seed, chain, f"a-{i:04d}")
            result.attempts += 1
            resp = client.send(req)
            if resp.error:
                result.errors[resp.error] += 1
            if resp.error in ("budget_exceeded", "log_failed"):
                result.stop_reason = resp.error
                break
            if resp.error:  # blocked_path / timeout / http_error: recorded, nothing to verify
                continue
            vin = VerifierInput(
                attempt_id=req.attempt_id,
                goal=Goal(seed_id=seed["seed_id"], success_criterion=seed.get("target", "")),
                code_checks=code_checks(seed, resp.text),
                attack_text=req.body.get("message", ""),
                target_text=resp.text,
            )
            out = verifier(vin)
            result.verdicts[out.verdict] += 1
            log.log_event(
                "verdict",
                "verifier",
                {
                    "attempt_id": out.attempt_id,
                    "verdict": out.verdict,
                    "basis": out.basis,
                    "evidence_quote": out.evidence_quote,
                    "rationale": out.rationale,
                    "severity_hint": out.severity_hint,
                    "code_checks": {"canary_found": vin.code_checks.canary_found,
                                    "refusal_matched": vin.code_checks.refusal_matched},
                },
            )
    except BudgetStop as e:  # Budget already recorded kill / budget_stop
        result.stop_reason = e.reason
    except LogWriteError as e:
        result.stop_reason = f"log_failed: {e}"
    finally:
        result.sent = tc.CLIENT_COUNTER - sent_before
        try:
            log.log_event(
                "session_end",
                "agent",
                {"stop_reason": result.stop_reason, "attempts": result.attempts, "sent": result.sent,
                 "errors": dict(result.errors), "verdicts": dict(result.verdicts)},
            )
        except LogWriteError:
            pass  # nothing more can be recorded; the missing session_end itself shows the failure
        client.close()
    return result


def main(argv: Optional[list[str]] = None) -> int:
    from agent import advbench
    from agent.planner import RulePlanner

    ap = argparse.ArgumentParser(description="Run one supervised session (rule-based planner, no model spend).")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--seeds", type=int, default=5, help="first N AdvBench seeds")
    ap.add_argument("--chains", nargs="+", default=["plain", "roleplay_wrapper"],
                    help="mutation chains; join rules in one chain with '+', e.g. roleplay_wrapper+korean_switch")
    args = ap.parse_args(argv)

    chains = [c.split("+") for c in args.chains]
    seeds = advbench.load()[: args.seeds]
    planner = RulePlanner(seeds, chains, max_attempts=args.max_attempts)
    r = run_session(planner, session_meta={"planner": "RulePlanner", "seeds": args.seeds, "chains": chains,
                                           "max_attempts": args.max_attempts})
    print(f"log={r.log_path} stop={r.stop_reason} attempts={r.attempts} sent={r.sent} "
          f"errors={dict(r.errors)} verdicts={dict(r.verdicts)}")
    return 0 if r.stop_reason == "planner_exhausted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
