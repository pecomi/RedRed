"""Planner: picks the next attempt. Keeps a per-run cap so a loop cannot run forever.

For the mid-check slice the Planner can run rule-based (cycle through seeds x chains) so the
loop works without model spend. The model-driven planner (reads prompts/planner.md and calls
the LLM) is added on top once budget prices are set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROMPT_PATH = Path("prompts/planner.md")

# Cap on attempts the Planner will propose in one run. The Budget still enforces the
# hard session limits; this is a smaller, planner-level stop so a bug cannot loop.
MAX_ATTEMPTS_PER_RUN = 50


def load_prompt(path: Path = PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8")


@dataclass
class RulePlanner:
    """Deterministic planner for the mid-check: walk seeds x mutation chains, capped."""
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
