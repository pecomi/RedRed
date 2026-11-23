"""Budget and kill switch. All operating limits live here and nowhere else (CLAUDE.md §4)."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# --- Operating limits (Proposal Defence #3, ROE §3) ---
MAX_TARGET_REQUESTS_PER_MIN = 10
MAX_TARGET_REQUESTS_PER_SESSION = 200  # retries count too
MAX_SESSION_SECONDS = 30 * 60
MAX_SESSION_COST_USD = 5.0

# --- Model prices, USD per million tokens.
# Anthropic's published rates for claude-opus-4-8, the default model (platform.claude.com,
# $5 / $25 per 1M tokens, read 2026-11; claude-opus-5 has the same rates). Check these
# when KU_LLM_MODEL changes. Overestimating cost only trips the USD 5 cap earlier, never later.
PRICE_INPUT_USD_PER_MTOK: Optional[float] = 5.0    # USD per 1M input tokens
PRICE_OUTPUT_USD_PER_MTOK: Optional[float] = 25.0  # USD per 1M output tokens

# --- Kill switch: the supervisor stops a running session by creating this file.
KILL_FILE = Path("logs/KILL")
# While waiting for the per-minute rate window, the kill file is checked at least this often.
WAIT_STEP_SECONDS = 1.0

# Logger interface agreed at kickoff: log_event(type, actor, data)
LogEvent = Callable[[str, str, dict], None]


def no_log(type_: str, actor: str, data: dict) -> None:
    """Stub used until the real Logger (agent/logger.py) is wired in."""


class BudgetStop(Exception):
    """A session limit was reached or the kill switch is on. The session must end."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def require_prices() -> None:
    if PRICE_INPUT_USD_PER_MTOK is None or PRICE_OUTPUT_USD_PER_MTOK is None:
        raise RuntimeError(
            "Model prices are not set in agent/budget.py; refusing to make a live model call."
        )


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    require_prices()
    return (
        input_tokens * PRICE_INPUT_USD_PER_MTOK + output_tokens * PRICE_OUTPUT_USD_PER_MTOK
    ) / 1_000_000


@dataclass
class Budget:
    log_event: LogEvent = no_log
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    kill_file: Path = KILL_FILE
    started_at: float = field(init=False)
    target_requests: int = field(init=False, default=0)
    cost: float = field(init=False, default=0.0)
    stopped: Optional[str] = field(init=False, default=None)
    _recent: deque = field(init=False, default_factory=deque)

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    def check(self) -> None:
        """Call before every model call and every target request. Raises BudgetStop."""
        if self.stopped:
            raise BudgetStop(self.stopped)
        if self.kill_file.exists():
            self._stop("kill", "kill switch file present")
        if self.clock() - self.started_at >= MAX_SESSION_SECONDS:
            self._stop("budget_stop", "session time limit")
        if self.cost >= MAX_SESSION_COST_USD:
            self._stop("budget_stop", "session cost limit")

    def acquire_target_request(self) -> None:
        """Call right before sending one target request.

        The per-minute rate is enforced by waiting, so it is never exceeded.
        Session limits (requests, time, cost) and the kill switch end the session.
        """
        self.check()
        if self.target_requests >= MAX_TARGET_REQUESTS_PER_SESSION:
            self._stop("budget_stop", "session request limit")
        self._drop_old()
        if len(self._recent) >= MAX_TARGET_REQUESTS_PER_MIN:
            self._wait_until(self._recent[0] + 60)
            self._drop_old()
        self._recent.append(self.clock())
        self.target_requests += 1

    def add_cost(self, usd: float) -> None:
        self.cost += usd

    def kill(self, reason: str = "manual") -> None:
        """Mark the session as killed. The next check() raises BudgetStop."""
        self._mark("kill", reason)

    def _wait_until(self, deadline: float) -> None:
        """Wait for the rate window in steps of at most WAIT_STEP_SECONDS, calling check() before
        each step, so the kill switch (or the time limit) ends the session within one step
        instead of after the whole wait."""
        while True:
            self.check()
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self.sleep(min(WAIT_STEP_SECONDS, remaining))

    def _drop_old(self) -> None:
        now = self.clock()
        while self._recent and now - self._recent[0] >= 60:
            self._recent.popleft()

    def _mark(self, type_: str, reason: str) -> None:
        if self.stopped:
            return
        self.stopped = reason
        self.log_event(
            type_,
            "budget",
            {
                "reason": reason,
                "target_requests": self.target_requests,
                "cost_usd": round(self.cost, 6),
                "elapsed_s": round(self.clock() - self.started_at, 1),
            },
        )

    def _stop(self, type_: str, reason: str) -> None:
        self._mark(type_, reason)
        raise BudgetStop(self.stopped)
