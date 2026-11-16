"""Append-only, hash-chained JSONL session log (CLAUDE.md §3, schemas/log_record.py fields).

Public API used by other components: ``log_event(type, actor, data)``.

- One event = one line. Existing lines are never modified or deleted.
- One file = one session. ``seq`` starts at 0 and increases by 1 with no gaps.
- ``hash = sha256(prev_hash + canonical JSON of the record without "hash")``;
  the first record uses ``GENESIS_HASH`` as ``prev_hash``. ``compute_hash`` and
  ``canonical_json`` are shared with the log checker script.
- A process restarted on the same file continues from its last line.
- Writes are serialised with a thread lock plus an exclusive ``flock`` on the file,
  so concurrent threads and processes cannot break ``seq`` or the chain.
- Any failure raises ``LogWriteError``. Callers must not send the request when that happens.
- Auth headers (``Authorization``, ``x-api-key``, ...) are replaced with ``[REDACTED]``.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Union

LOG_VERSION = 1
GENESIS_HASH = "0" * 64
REDACTED = "[REDACTED]"

EVENT_TYPES = frozenset(
    {
        "session_start",
        "model_request",
        "model_response",
        "target_request",
        "target_response",
        "verdict",
        "egress_denied",
        "budget_stop",
        "kill",
        "session_end",
    }
)

# Compared case-insensitively against dict keys at any depth of ``data``.
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "api_key",
        "apikey",
        "cookie",
        "set-cookie",
    }
)


class LogWriteError(RuntimeError):
    """The event could not be recorded. The caller must stop (e.g. not send the request)."""


class LogIntegrityError(LogWriteError):
    """The existing log file is malformed or its hash chain does not verify."""


# --- Hashing (shared with the checker script) ---------------------------------------


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 kept as-is, no NaN/Infinity."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def compute_hash(record: dict) -> str:
    """sha256(prev_hash + canonical JSON of the record without its "hash" field), hex."""
    body = {k: v for k, v in record.items() if k != "hash"}
    payload = record["prev_hash"] + canonical_json(body)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_chain(path: Union[str, Path]) -> int:
    """Check seq continuity, session_id consistency and the hash chain. Returns the record count."""
    expected_prev = GENESIS_HASH
    session_id = None
    count = 0
    for count, rec in enumerate(iter_records(path), start=1):
        seq = count - 1
        if rec.get("seq") != seq:
            raise LogIntegrityError(f"line {count}: seq {rec.get('seq')!r}, expected {seq}")
        if session_id is None:
            session_id = rec.get("session_id")
        elif rec.get("session_id") != session_id:
            raise LogIntegrityError(f"line {count}: session_id changed within one file")
        if rec.get("prev_hash") != expected_prev:
            raise LogIntegrityError(f"line {count}: prev_hash does not match previous hash")
        if compute_hash(rec) != rec.get("hash"):
            raise LogIntegrityError(f"line {count}: hash mismatch")
        expected_prev = rec["hash"]
    return count


def iter_records(path: Union[str, Path]) -> Iterator[dict]:
    with open(path, "rb") as f:
        for lineno, raw in enumerate(f, start=1):
            if not raw.endswith(b"\n"):
                raise LogIntegrityError(f"line {lineno}: incomplete last line")
            try:
                yield json.loads(raw)
            except ValueError as e:
                raise LogIntegrityError(f"line {lineno}: not valid JSON") from e


# --- Helpers --------------------------------------------------------------------------


def _utc_now_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and k.lower() in SENSITIVE_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _read_last_line(f) -> Optional[bytes]:
    """Last complete line of a binary file opened for reading, or None if the file is empty."""
    f.seek(0, os.SEEK_END)
    end = f.tell()
    if end == 0:
        return None
    f.seek(end - 1)
    if f.read(1) != b"\n":
        raise LogIntegrityError("log file does not end with a newline (interrupted write?)")
    # Walk backwards from just before the trailing newline until the previous newline.
    pos = end - 1
    buf = b""
    while pos > 0:
        step = min(4096, pos)
        pos -= step
        f.seek(pos)
        buf = f.read(step) + buf
        idx = buf.rfind(b"\n")
        if idx != -1:
            return buf[idx + 1 :] + b"\n"
    return buf + b"\n"


# --- Logger ---------------------------------------------------------------------------


class Logger:
    """Writes one session's events to ``path``. Safe across threads and processes."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._lock = threading.Lock()

    def start_session(self, session_id: Optional[str] = None, data: Optional[dict] = None) -> dict:
        """Write the session_start record. The file must be empty (one file = one session)."""
        payload = dict(data or {})
        if session_id is not None:
            payload["session_id"] = session_id
        return self.log_event("session_start", "agent", payload)

    def log_event(self, type: str, actor: str, data: dict) -> dict:  # noqa: A002 (agreed signature)
        if type not in EVENT_TYPES:
            raise LogWriteError(f"unknown event type: {type!r}")
        if not isinstance(actor, str) or not actor:
            raise LogWriteError("actor must be a non-empty string")
        if not isinstance(data, dict):
            raise LogWriteError("data must be a dict")

        data = _redact(copy.deepcopy(data))
        attempt_id = data.pop("attempt_id", None)
        requested_session = data.pop("session_id", None) if type == "session_start" else None

        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a+b") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        record = self._build(f, type, actor, attempt_id, data, requested_session)
                        line = (canonical_json(record) + "\n").encode("utf-8")
                        f.seek(0, os.SEEK_END)
                        f.write(line)
                        f.flush()
                        os.fsync(f.fileno())
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except LogWriteError:
            raise
        except (OSError, TypeError, ValueError) as e:
            raise LogWriteError(f"failed to write log event {type!r} to {self.path}: {e}") from e
        return record

    def _build(self, f, type_, actor, attempt_id, data, requested_session) -> dict:
        last_raw = _read_last_line(f)
        if last_raw is None:
            if type_ != "session_start":
                raise LogWriteError("log file is empty: the first event must be session_start")
            session_id = requested_session or uuid.uuid4().hex
            seq, prev_hash = 0, GENESIS_HASH
        else:
            if type_ == "session_start":
                raise LogWriteError("session already started in this file; use a new log path")
            try:
                last = json.loads(last_raw)
                session_id, seq, prev_hash = last["session_id"], last["seq"] + 1, last["hash"]
            except (ValueError, KeyError, TypeError) as e:
                raise LogIntegrityError("cannot parse the last line of the log file") from e
            if compute_hash(last) != prev_hash:
                raise LogIntegrityError("last record's hash does not verify")

        record = {
            "v": LOG_VERSION,
            "session_id": session_id,
            "seq": seq,
            "ts": _utc_now_ms(),
            "type": type_,
            "actor": actor,
            "attempt_id": attempt_id,
            "data": data,
            "prev_hash": prev_hash,
        }
        record["hash"] = compute_hash(record)
        return record


# --- Module-level default logger (path from config) ------------------------------------

_default: Optional[Logger] = None
_default_lock = threading.Lock()


def get_logger() -> Logger:
    global _default
    with _default_lock:
        if _default is None:
            from config import get_settings

            _default = Logger(get_settings().log_path)
        return _default


def log_event(type: str, actor: str, data: dict) -> dict:  # noqa: A002 (agreed signature)
    """Record one event in the session log. Raises LogWriteError on any failure."""
    return get_logger().log_event(type, actor, data)


def start_session(session_id: Optional[str] = None, data: Optional[dict] = None) -> dict:
    return get_logger().start_session(session_id, data)
