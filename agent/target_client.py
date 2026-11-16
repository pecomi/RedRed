"""Target client: the only code that sends requests to the staging target.

- The base URL comes only from config.get_settings().staging_target_url. A TargetRequest
  carries a path and body, never a host, so the generator cannot pick where traffic goes.
- send() checks in this fixed order (kickoff contract D):
    1. path / host check  -> egress_denied log, error "blocked_path"
    2. budget check       -> budget_stop log,   error "budget_exceeded"
    3. log target_request -> if the write fails, nothing is sent, error "log_failed"
    4. send through Squid -> target_response log on success and on failure
- Inside the sandbox, httpx follows HTTP_PROXY from docker-compose, so traffic goes through Squid.
  The agent container has no other route out, so a missing proxy setting fails closed.
- CLIENT_COUNTER counts every send that was actually attempted (seam 3: sent == logged).
- Logging defaults to agent.logger.log_event, which needs start_session() first; until then
  every write fails, so nothing is sent (fail closed). budget_check has no default on purpose.
- When a Budget built with the same logger runs out, budget_stop is recorded twice: once by
  Budget (session level) and once here (attempt level, with attempt_id). Counts are unaffected.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

from agent.budget import Budget, BudgetStop
from config import get_settings
from schemas.target_io import TargetRequest, TargetResponse

ALLOWED_METHODS = ("GET", "POST")
_FORBIDDEN_IN_PATH = ("//", "://", "@", "\\")

# Logger interface: log_event(type, actor, data). agent.logger returns the record and raises
# LogWriteError on failure. A raise or a False return both count as a failed write.
LogEvent = Callable[[str, str, dict], object]
# Returns a reason string when the budget is exhausted, otherwise None.
BudgetCheck = Callable[[], Optional[str]]

CLIENT_COUNTER = 0


def budget_check_from(budget: Budget) -> BudgetCheck:
    """Adapt agent.budget.Budget (raises BudgetStop) to the BudgetCheck interface."""

    def check() -> Optional[str]:
        try:
            budget.acquire_target_request()
        except BudgetStop as e:
            return e.reason
        return None

    return check


def check_egress(base_url: str, method: str, path: str) -> Optional[str]:
    """Return a denial reason, or None if the request may go out to base_url + path."""
    if not base_url:
        return "staging_target_url is not configured"
    if method not in ALLOWED_METHODS:
        return f"method {method!r} not allowed"
    if not isinstance(path, str) or not path.startswith("/"):
        return "path must start with '/'"
    for bad in _FORBIDDEN_IN_PATH:
        if bad in path:
            return f"path contains {bad!r}"
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        return "path contains whitespace or control characters"
    url = build_url(base_url, path)
    if urlsplit(url).netloc != urlsplit(base_url).netloc:
        return "assembled URL leaves the configured host"
    return None


def build_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def extract_text(body_raw: str) -> tuple[str, Optional[str]]:
    """Pull the answer text and conversation id out of the target's response body.

    The text is untrusted data; callers pass it on only through wrap_untrusted().
    """
    try:
        data = json.loads(body_raw)
    except ValueError:
        return body_raw, None
    if not isinstance(data, dict):
        return body_raw, None
    text = next((data[k] for k in ("reply", "text", "message", "response") if isinstance(data.get(k), str)), body_raw)
    conv = data.get("conversation_id")
    return text, conv if isinstance(conv, str) else None


def _logged(log_event: LogEvent, type_: str, data: dict) -> bool:
    try:
        return log_event(type_, "target_client", data) is not False
    except Exception:
        return False


class TargetClient:
    def __init__(
        self,
        budget_check: BudgetCheck,
        log_event: Optional[LogEvent] = None,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = get_settings().staging_target_url
        if log_event is None:
            # Imported here, not at module level: agent.logger needs fcntl (POSIX only),
            # and offline tests of this module also run on Windows.
            from agent.logger import log_event
        self._log = log_event
        self._budget_check = budget_check
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)

    def send(self, req: TargetRequest) -> TargetResponse:
        global CLIENT_COUNTER

        def fail(error: str, status: int = 0, latency_ms: int = 0, body_raw: str = "") -> TargetResponse:
            return TargetResponse(
                attempt_id=req.attempt_id, turn=req.turn, status=status, text="", body_raw=body_raw,
                conversation_id=req.conversation_id, latency_ms=latency_ms, error=error,
            )

        # 1. Path / host check
        reason = check_egress(self._base_url, req.method, req.path)
        if reason:
            _logged(self._log, "egress_denied", {"attempt_id": req.attempt_id, "reason": reason, "request": req.to_log()})
            return fail("blocked_path")

        # 2. Budget check
        reason = self._budget_check()
        if reason:
            _logged(self._log, "budget_stop", {"attempt_id": req.attempt_id, "reason": reason})
            return fail("budget_exceeded")

        # 3. Log before sending. No log, no send.
        url = build_url(self._base_url, req.path)
        if not _logged(self._log, "target_request", {"attempt_id": req.attempt_id, "url": url, "request": req.to_log()}):
            return fail("log_failed")

        # 4. Send through Squid
        CLIENT_COUNTER += 1
        body = dict(req.body)
        if req.conversation_id is not None:
            body.setdefault("conversation_id", req.conversation_id)
        t0 = time.monotonic()
        try:
            if req.method == "GET":
                r = self._http.get(url, params=req.body or None)
            else:
                r = self._http.post(url, json=body)
            latency_ms = round((time.monotonic() - t0) * 1000)
            if r.status_code >= 400:
                resp = fail("http_error", r.status_code, latency_ms, r.text)
            else:
                text, conv = extract_text(r.text)
                resp = TargetResponse(
                    attempt_id=req.attempt_id, turn=req.turn, status=r.status_code, text=text, body_raw=r.text,
                    conversation_id=conv or req.conversation_id, latency_ms=latency_ms, error=None,
                )
        except httpx.TimeoutException:
            resp = fail("timeout", latency_ms=round((time.monotonic() - t0) * 1000))
        except httpx.HTTPError as e:
            resp = fail("http_error", latency_ms=round((time.monotonic() - t0) * 1000), body_raw=type(e).__name__)

        _logged(self._log, "target_response", resp.to_log())
        return resp

    def close(self) -> None:
        self._http.close()
