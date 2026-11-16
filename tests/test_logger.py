"""Offline tests for agent/logger.py. No network, no .env needed."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import logger as logmod
from agent.logger import (
    GENESIS_HASH,
    LogIntegrityError,
    Logger,
    LogWriteError,
    compute_hash,
    verify_chain,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def read_lines(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def assert_valid_session(path: Path, n: int) -> list:
    recs = read_lines(path)
    assert [r["seq"] for r in recs] == list(range(n))
    assert recs[0]["prev_hash"] == GENESIS_HASH
    for prev, cur in zip(recs, recs[1:]):
        assert cur["prev_hash"] == prev["hash"]
    for r in recs:
        assert compute_hash(r) == r["hash"]
    assert len({r["session_id"] for r in recs}) == 1
    assert verify_chain(path) == n
    return recs


# --- Done criterion 1 -------------------------------------------------------------------


def test_100_records_seq_and_chain(tmp_path):
    path = tmp_path / "s.jsonl"
    lg = Logger(path)
    lg.start_session("sess-1")
    for i in range(99):
        lg.log_event("model_request", "planner", {"attempt_id": f"a{i}", "i": i})
    recs = assert_valid_session(path, 100)
    assert recs[0]["type"] == "session_start" and recs[0]["session_id"] == "sess-1"
    assert set(recs[0]) == {
        "v", "session_id", "seq", "ts", "type", "actor", "attempt_id", "data", "prev_hash", "hash",
    }


# --- Done criterion 2 -------------------------------------------------------------------


def test_concurrent_threads_keep_seq_and_chain(tmp_path):
    path = tmp_path / "s.jsonl"
    lg = Logger(path)
    lg.start_session()
    barrier = threading.Barrier(9)
    errors = []

    def worker(t):
        try:
            barrier.wait()
            for i in range(11):
                lg.log_event("target_request", f"t{t}", {"attempt_id": f"{t}-{i}"})
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(9)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    recs = assert_valid_session(path, 100)
    assert len({r["attempt_id"] for r in recs[1:]}) == 99


def test_separate_logger_instances_share_file_lock(tmp_path):
    """Two Logger objects (as in two processes) on one file still produce a valid chain."""
    path = tmp_path / "s.jsonl"
    Logger(path).start_session()
    a, b = Logger(path), Logger(path)

    def worker(lg, name):
        for i in range(33):
            lg.log_event("verdict", name, {"i": i})

    ts = [threading.Thread(target=worker, args=(lg, n)) for lg, n in ((a, "a"), (b, "b"), (a, "c"))]
    for th in ts:
        th.start()
    for th in ts:
        th.join()
    assert_valid_session(path, 100)


# --- Done criterion 3 -------------------------------------------------------------------


def test_unwritable_path_raises(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    lg = Logger(blocker / "s.jsonl")  # parent is a regular file: always unwritable
    with pytest.raises(LogWriteError):
        lg.start_session()


def test_readonly_directory_raises(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root; directory permissions are not enforced")
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        with pytest.raises(LogWriteError):
            Logger(ro / "s.jsonl").start_session()
    finally:
        ro.chmod(0o700)


def test_unserialisable_data_raises_and_writes_nothing(tmp_path):
    path = tmp_path / "s.jsonl"
    lg = Logger(path)
    lg.start_session()
    with pytest.raises(LogWriteError):
        lg.log_event("model_request", "planner", {"x": object()})
    with pytest.raises(LogWriteError):
        lg.log_event("model_request", "planner", {"x": float("nan")})
    assert_valid_session(path, 1)


# --- Done criterion 4 -------------------------------------------------------------------


def _child_write(path: Path, n: int, start: bool) -> None:
    code = (
        "import sys\n"
        "from agent.logger import Logger\n"
        "lg = Logger(sys.argv[1])\n"
        "if sys.argv[3] == '1':\n"
        "    lg.start_session('sess-restart')\n"
        "for i in range(int(sys.argv[2])):\n"
        "    lg.log_event('target_response', 'target_client', {'i': i})\n"
    )
    subprocess.run(
        [sys.executable, "-c", code, str(path), str(n), "1" if start else "0"],
        cwd=REPO_ROOT,
        check=True,
        timeout=60,
    )


def test_restart_continues_seq_and_chain(tmp_path):
    path = tmp_path / "s.jsonl"
    _child_write(path, 49, start=True)   # process 1: session_start + 49 = 50 records
    _child_write(path, 50, start=False)  # process 2 (restart): 50 more
    recs = assert_valid_session(path, 100)
    assert recs[50]["prev_hash"] == recs[49]["hash"]
    assert {r["session_id"] for r in recs} == {"sess-restart"}


def test_existing_lines_are_not_modified(tmp_path):
    path = tmp_path / "s.jsonl"
    lg = Logger(path)
    lg.start_session()
    lg.log_event("verdict", "verifier", {})
    before = path.read_bytes()
    Logger(path).log_event("session_end", "agent", {})
    assert path.read_bytes().startswith(before)


# --- Other guarantees -------------------------------------------------------------------


def test_first_event_must_be_session_start(tmp_path):
    with pytest.raises(LogWriteError):
        Logger(tmp_path / "s.jsonl").log_event("model_request", "planner", {})


def test_second_session_start_in_same_file_rejected(tmp_path):
    lg = Logger(tmp_path / "s.jsonl")
    lg.start_session()
    with pytest.raises(LogWriteError):
        lg.start_session()


def test_unknown_type_rejected(tmp_path):
    lg = Logger(tmp_path / "s.jsonl")
    lg.start_session()
    with pytest.raises(LogWriteError):
        lg.log_event("bogus", "planner", {})


def test_attempt_id_lifted_from_data_and_caller_dict_untouched(tmp_path):
    lg = Logger(tmp_path / "s.jsonl")
    lg.start_session()
    data = {"attempt_id": "A1", "body": "hi"}
    rec = lg.log_event("model_request", "planner", data)
    assert rec["attempt_id"] == "A1" and rec["data"] == {"body": "hi"}
    assert data == {"attempt_id": "A1", "body": "hi"}
    assert lg.log_event("verdict", "verifier", {})["attempt_id"] is None


def test_auth_headers_redacted(tmp_path):
    path = tmp_path / "s.jsonl"
    lg = Logger(path)
    lg.start_session()
    lg.log_event(
        "target_request",
        "target_client",
        {"headers": {"Authorization": "Bearer sk-secret", "X-API-Key": "k-secret", "Accept": "a"},
         "nested": [{"api_key": "k2-secret"}]},
    )
    text = path.read_text(encoding="utf-8")
    assert "secret" not in text
    rec = read_lines(path)[1]
    assert rec["data"]["headers"] == {"Authorization": "[REDACTED]", "X-API-Key": "[REDACTED]", "Accept": "a"}


def test_tampered_line_detected(tmp_path):
    path = tmp_path / "s.jsonl"
    lg = Logger(path)
    lg.start_session()
    lg.log_event("verdict", "verifier", {"verdict": "not_finding"})
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace("not_finding", "finding")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(LogIntegrityError):
        verify_chain(path)
    with pytest.raises(LogIntegrityError):
        Logger(path).log_event("session_end", "agent", {})


def test_truncated_last_line_refuses_to_append(tmp_path):
    path = tmp_path / "s.jsonl"
    Logger(path).start_session()
    with open(path, "ab") as f:
        f.write(b'{"partial":')
    with pytest.raises(LogIntegrityError):
        Logger(path).log_event("session_end", "agent", {})


def test_ts_is_utc_iso_ms(tmp_path):
    rec = Logger(tmp_path / "s.jsonl").start_session()
    assert len(rec["ts"]) == len("2026-11-16T03:04:05.123Z") and rec["ts"].endswith("Z")


def test_module_log_event_uses_config_path(tmp_path, monkeypatch):
    path = tmp_path / "from_config.jsonl"
    import config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(log_path=str(path)))
    monkeypatch.setattr(logmod, "_default", None)
    logmod.start_session("cfg")
    logmod.log_event("session_end", "agent", {})
    assert_valid_session(path, 2)
