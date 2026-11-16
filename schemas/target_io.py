"""Target client contract (Interface spec v0, section 2).

The generator produces a TargetRequest with a path and body only, never a host.
The Target client fixes the base URL from config and does the checks in target_client.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Method = Literal["GET", "POST"]
# log_failed: the target_request log write failed, so the request was not sent (added for the Target client).
TargetError = Literal["timeout", "http_error", "blocked_path", "budget_exceeded", "log_failed"]


@dataclass(frozen=True)
class TargetRequest:
    attempt_id: str
    path: str                      # starts with "/", no host
    method: Method = "POST"
    body: dict = field(default_factory=dict)
    turn: int = 1
    conversation_id: Optional[str] = None
    seed_id: Optional[str] = None
    mutations: list[str] = field(default_factory=list)

    def to_log(self) -> dict:
        return {
            "attempt_id": self.attempt_id,
            "turn": self.turn,
            "method": self.method,
            "path": self.path,
            "body": self.body,
            "conversation_id": self.conversation_id,
            "seed_id": self.seed_id,
            "mutations": self.mutations,
        }


@dataclass(frozen=True)
class TargetResponse:
    attempt_id: str
    turn: int
    status: int                    # HTTP status, or 0 when the request never went out
    text: str                      # answer text the adapter extracted; Planner/Verifier use this
    body_raw: str = ""             # raw response body; log only
    conversation_id: Optional[str] = None
    latency_ms: int = 0
    error: Optional[TargetError] = None

    def to_log(self) -> dict:
        return {
            "attempt_id": self.attempt_id,
            "turn": self.turn,
            "status": self.status,
            "body_raw": self.body_raw,
            "text": self.text,
            "conversation_id": self.conversation_id,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }
