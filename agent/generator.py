"""Attack generator: turn AdvBench seeds into TargetRequests, one per attempt.

For the mid-check slice this uses the offline mutation rules only, which keeps the
loop runnable without spending budget. Each request records its seed and the
mutation chain, so every variant traces back to a seed and a rule set (provenance).
"""
from __future__ import annotations

import itertools
from typing import Iterable, Iterator, Optional

from agent.mutations.rules import apply_chain
from schemas.target_io import TargetRequest

# The staging target's single endpoint (CLAUDE.md / interface spec): POST /chat {"message", ...}.
TARGET_PATH = "/chat"


def build_request(seed: dict, mutations: list[str], attempt_id: str) -> TargetRequest:
    message = apply_chain(seed["goal"], mutations)
    return TargetRequest(
        attempt_id=attempt_id,
        path=TARGET_PATH,
        method="POST",
        body={"message": message},
        seed_id=seed["seed_id"],
        mutations=mutations,
    )


def generate(
    seeds: Iterable[dict],
    mutation_chains: list[list[str]],
    *,
    limit: Optional[int] = None,
    start: int = 0,
) -> Iterator[TargetRequest]:
    """Cross seeds with mutation chains, yielding one TargetRequest per pair.

    attempt_id is a-0000, a-0001, ... so it is stable and sortable in the log.
    `limit` caps how many are produced (useful to stay well inside the budget).
    """
    counter = itertools.count(start)
    made = 0
    for seed in seeds:
        for chain in mutation_chains:
            if limit is not None and made >= limit:
                return
            yield build_request(seed, chain, f"a-{next(counter):04d}")
            made += 1
