"""Planner: chooses which (seed, mutation chain) to try next.

Two planners, same output shape so run_session can take either:

- RulePlanner: deterministic, walks seeds x mutation chains, capped. No model spend.
  Used for the mid-check slice so the loop runs for free.
- LLMPlanner: Claude plans the next attempt from the goal, the available mutation rules and
  a short history of past attempts and their verdicts (reads prompts/planner.md, calls the
  LLM through the session's LLMClient, so its calls share the session budget and log).

Both yield (seed, chain) pairs. LLMPlanner is iterated one attempt at a time: it is given
the verdict of the previous attempt before it proposes the next (feedback() ).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Protocol

from agent.mutations.rules import REGISTRY

PROMPT_PATH = Path("prompts/planner.md")

# Cap on attempts a planner proposes in one run. Budget still enforces the hard session
# limits; this is a smaller planner-level stop so a bug cannot loop forever.
MAX_ATTEMPTS_PER_RUN = 50


def load_prompt(path: Path = PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8")


@dataclass
class RulePlanner:
    """Deterministic planner: walk seeds x mutation chains, capped."""
    seeds: list[dict]
    mutation_chains: list[list[str]]
    max_attempts: int = MAX_ATTEMPTS_PER_RUN
    _plan: list[tuple[dict, list[str]]] = field(init=False)

    def __post_init__(self) -> None:
        self._plan = [(s, c) for s in self.seeds for c in self.mutation_chains]
        if self.max_attempts >= 0:
            self._plan = self._plan[: self.max_attempts]

    def __iter__(self):
        return iter(self._plan)

    def __len__(self) -> int:
        return len(self._plan)


class _Completer(Protocol):
    def complete(self, role: str, system: str, messages: list[dict], **kw) -> object: ...


@dataclass
class LLMPlanner:
    """Claude proposes the next (seed, chain). Iterated one attempt at a time.

    The runner calls feedback(verdict) after each attempt (run_session can be adapted to,
    or use via plan_next directly). Kept simple and model-agnostic: the LLM returns JSON
    {"seed_id","mutations","rationale"}; unknown seeds or rules fall back to a safe default.
    """
    seeds: list[dict]
    llm: _Completer
    allowed_mutations: list[str] = field(default_factory=lambda: sorted(REGISTRY))
    max_attempts: int = MAX_ATTEMPTS_PER_RUN
    system_prompt: str = field(default_factory=load_prompt)
    _by_id: dict = field(init=False)
    _history: list[dict] = field(init=False, default_factory=list)
    _made: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._by_id = {s["seed_id"]: s for s in self.seeds}

    def feedback(self, seed_id: str, mutations: list[str], verdict: str) -> None:
        """Record the result of the attempt just run, so the next plan can use it."""
        self._history.append({"seed_id": seed_id, "mutations": mutations, "verdict": verdict})

    def plan_next(self, attempt_id: str) -> Optional[tuple[dict, list[str]]]:
        """Ask the model for the next (seed, chain). None when the run is done."""
        if self._made >= self.max_attempts:
            return None
        user = json.dumps(
            {
                "seeds": [{"seed_id": s["seed_id"], "goal": s["goal"]} for s in self.seeds],
                "allowed_mutations": self.allowed_mutations,
                "history": self._history[-10:],
            },
            ensure_ascii=False,
        )
        result = self.llm.complete(
            "planner", self.system_prompt, [{"role": "user", "content": user}],
            attempt_id=attempt_id, max_tokens=256, temperature=1.0,
        )
        seed, chain = self._parse(getattr(result, "text", ""))
        self._made += 1
        return seed, chain

    def _parse(self, text: str) -> tuple[dict, list[str]]:
        """Parse the model's JSON; fall back to a valid default on anything malformed."""
        default_seed = self.seeds[0]
        try:
            start, end = text.index("{"), text.rindex("}") + 1
            obj = json.loads(text[start:end])
            seed = self._by_id.get(obj.get("seed_id"), default_seed)
            muts = [m for m in obj.get("mutations", []) if m in REGISTRY] or ["plain"]
            return seed, muts
        except (ValueError, KeyError, TypeError):
            return default_seed, ["plain"]

    def __iter__(self) -> Iterator[tuple[dict, list[str]]]:
        """Iterate without feedback (each plan sees only history fed via feedback())."""
        i = 0
        while True:
            nxt = self.plan_next(f"a-{i:04d}")
            if nxt is None:
                return
            yield nxt
            i += 1
