"""Session runner: one supervised session of the full loop, inside the sandbox.

This file owns only the order of calls and the session lifecycle. The parts are injected:
  planner     iterable of (seed, mutation_chain)        Jiseon  (agent/planner.py)
  build       (seed, chain, attempt_id) -> TargetRequest Jiseon  (agent/generator.py)
  code_checks (seed, target_text) -> CodeChecks          Soona   (agent/verifier.py)
  verifier    VerifierInput -> VerifierOutput            Soona   (agent/verifier.py)
run_session() keeps stand-ins as defaults; main() wires the Verifier's code checks.
LLM injection point: `make_verifier(budget, log_event)` is called once this session's Budget and
Logger exist, so a model judge (agent.llm.LLMClient(budget, log_event)) spends from the same
session budget and writes model_request / model_response into the same session log. It returns
the verifier, and its `code_checks` method (if any) is used as code_checks.
`make_planner(budget, log_event)` is the same injection point for a model planner (LLMPlanner):
pass it instead of `planner`, so the planner's model calls also spend from this session's budget
and are logged in this session's log.
Target client, Logger and Budget are wired here.

Per attempt: kill/budget check -> build request -> send (Target client) -> verify -> log verdict.
A planner with a feedback(seed_id, mutations, verdict) method (e.g. LLMPlanner) is given each
verdict before it proposes the next attempt; a plain iterable planner (RulePlanner) is unaffected.
The session stops on: planner exhausted, budget limit or kill switch (Budget records
budget_stop / kill), any log write failure (CLAUDE.md §4), or a failed planner model call
(stop_reason "planner_failed: <error type>"). If anything else raises, session_end records
"crashed: <error type>" and the error is re-raised. One session = one new log file.

Usage (inside the sandbox; sends real requests to the staging target, so a human confirms first):
  docker compose exec agent python -m agent.run_session --max-attempts 3
  docker compose exec agent python -m agent.run_session --planner llm --max-attempts 3   # live BAZE calls, billed
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
LogEvent = Callable[[str, str, dict], object]
MakeVerifier = Callable[[Budget, LogEvent], Verifier]
MakePlanner = Callable[[Budget, LogEvent], Planner]


def no_code_checks(seed: dict, target_text: str) -> CodeChecks:
    """Default stand-in: no code checks. main() passes agent.verifier.Verifier.code_checks."""
    return CodeChecks()


def stub_verifier(vin: VerifierInput) -> VerifierOutput:
    """Default stand-in. Never claims a finding. main() passes agent.verifier.Verifier."""
    return VerifierOutput(
        attempt_id=vin.attempt_id, verdict="uncertain", basis="code", rationale="verifier not wired yet"
    )


# --- CHANGE 1 of 3: step a planner one attempt at a time -------------------------------
# A feedback planner (LLMPlanner) chooses the next attempt from past verdicts, so it must be
# pulled one at a time via plan_next(); a plain iterable planner (RulePlanner) is listed once
# and indexed. Either way run_session gets one (seed, chain) per call, or None when done.
def _next_attempt(planner, i: int):
    if hasattr(planner, "plan_next"):              # LLMPlanner: ask for the next, given history so far
        return planner.plan_next(f"a-{i:04d}")
    seq = getattr(planner, "_run_session_list", None)
    if seq is None:
        seq = list(planner)                        # RulePlanner / any iterable: expand once
        planner._run_session_list = seq
    return seq[i] if i < len(seq) else None
# ---------------------------------------------------------------------------------------


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
    planner: Optional[Planner] = None,
    *,
    make_planner: Optional[MakePlanner] = None,
    build: Build = build_request,
    code_checks: CodeCheck = no_code_checks,
    verifier: Verifier = stub_verifier,
    make_verifier: Optional[MakeVerifier] = None,
    log_dir: Path = Path("logs"),
    kill_file: Path = budget_mod.KILL_FILE,
    transport: Optional[httpx.BaseTransport] = None,
    session_meta: Optional[dict] = None,
) -> SessionResult:
    if (planner is None) == (make_planner is None):
        raise ValueError("pass exactly one of planner or make_planner")
    log = Logger(new_log_path(log_dir))
    log.start_session(data={"runner": "agent.run_session", **(session_meta or {})})
    budget = Budget(log_event=log.log_event, kill_file=kill_file)
    client = tc.TargetClient(tc.budget_check_from(budget), log.log_event, transport=transport)
    sent_before = tc.CLIENT_COUNTER
    result = SessionResult(log_path=log.path, stop_reason="planner_exhausted")

    try:
        if make_verifier is not None:  # built here so a model judge shares this session's Budget and Logger
            verifier = make_verifier(budget, log.log_event)
            code_checks = getattr(verifier, "code_checks", code_checks)
        if make_planner is not None:  # same reason: a model planner shares this session's Budget and Logger
            planner = make_planner(budget, log.log_event)
        # --- CHANGE 2 of 3: drive the planner one step at a time (was: for i, (seed, chain) in enumerate(planner))
        i = 0
        while True:
            try:
                step = _next_attempt(planner, i)
            except (BudgetStop, LogWriteError):
                raise  # handled below; LogWriteError is a RuntimeError, so it must go first
            except (httpx.HTTPError, RuntimeError, ValueError) as e:
                # A planner model call failed (gateway error or timeout, bad response body, prices
                # not set). Stop the session with that reason instead of guessing an attempt.
                result.stop_reason = f"planner_failed: {type(e).__name__}"
                break
            if step is None:
                break
            seed, chain = step
            # -------------------------------------------------------------------------------
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
                i += 1
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
            # --- CHANGE 3 of 3: give the verdict back to a feedback planner (LLMPlanner) ---
            if hasattr(planner, "feedback"):
                planner.feedback(seed["seed_id"], chain, out.verdict)
            # -------------------------------------------------------------------------------
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
            i += 1
    except BudgetStop as e:  # Budget already recorded kill / budget_stop
        result.stop_reason = e.reason
    except LogWriteError as e:
        result.stop_reason = f"log_failed: {e}"
    except Exception as e:
        # Unexpected: record why the session ended (not "planner_exhausted") and let it surface.
        result.stop_reason = f"crashed: {type(e).__name__}"
        raise
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
    from agent.planner import LLMPlanner, RulePlanner
    from agent.llm import LLMClient
    from agent.verifier import Verifier
    from config import get_settings

    ap = argparse.ArgumentParser(description="Run one supervised session.")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--seeds", type=int, default=5, help="first N AdvBench seeds")
    ap.add_argument("--planner", choices=["rule", "llm"], default="rule",
                    help="rule: RulePlanner, no model spend. llm: LLMPlanner picks each attempt from past "
                         "verdicts (live BAZE calls, billed; a human confirms first).")
    ap.add_argument("--chains", nargs="+", default=["plain", "roleplay_wrapper"],
                    help="mutation chains for --planner rule; join rules in one chain with '+', "
                         "e.g. roleplay_wrapper+korean_switch. Ignored by --planner llm, which picks from all rules.")
    ap.add_argument("--judge", action="store_true",
                    help="also use the model judge (live BAZE calls, billed; a human confirms first). "
                         "Needs model prices in agent/budget.py, otherwise every judged attempt is uncertain.")
    args = ap.parse_args(argv)

    chains = [c.split("+") for c in args.chains]
    seeds = advbench.load()[: args.seeds]
    if args.planner == "llm":
        # Built inside run_session so its model calls share this session's Budget and Logger.
        planner_parts = {"make_planner": lambda budget, log_event: LLMPlanner(
            seeds, LLMClient(budget, log_event), max_attempts=args.max_attempts)}
        planner_meta = {"planner": "LLMPlanner"}
    else:
        planner_parts = {"planner": RulePlanner(seeds, chains, max_attempts=args.max_attempts)}
        planner_meta = {"planner": "RulePlanner", "chains": chains}
    canary = get_settings().staging_target_canary
    if not canary:
        print("WARNING: STAGING_TARGET_CANARY is empty; the canary check is off, so no attempt can be a finding.")

    if args.judge:
        # The judge must share this session's Budget and Logger, which exist only inside run_session.
        parts = {"make_verifier": lambda budget, log_event: Verifier(llm=LLMClient(budget, log_event), canary=canary)}
    else:
        verifier = Verifier(canary=canary)  # code checks only, no model spend
        parts = {"code_checks": verifier.code_checks, "verifier": verifier}

    r = run_session(**planner_parts, **parts,
                    session_meta={**planner_meta, "seeds": args.seeds,
                                  "max_attempts": args.max_attempts,
                                  "verifier": "code_checks+llm_judge" if args.judge else "code_checks_only",
                                  "canary_check": bool(canary)})
    print(f"log={r.log_path} stop={r.stop_reason} attempts={r.attempts} sent={r.sent} "
          f"errors={dict(r.errors)} verdicts={dict(r.verdicts)}")
    return 0 if r.stop_reason == "planner_exhausted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
