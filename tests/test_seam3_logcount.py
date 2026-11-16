"""Seam 3 (Logger <-> Target client): requests sent == target_request records logged.

Runs a mixed session (successes, blocked paths, budget stop, a log failure, HTTP errors)
against a mocked target with the real hash-chained Logger, then compares the log with
CLIENT_COUNTER and with what the mock target actually received.
"""
import httpx
import pytest

import config
from agent import target_client as tc
from schemas.target_io import TargetRequest

pytest.importorskip("fcntl", reason="agent.logger is POSIX-only; run in the container")
from agent.logger import Logger, LogWriteError, iter_records, verify_chain  # noqa: E402


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("KU_LLM_API_KEY", "sk-test-NEVER-LOG-ME")
    monkeypatch.setenv("KU_LLM_BASE_URL", "https://api.baze.co/v1/gateway/claude")
    monkeypatch.setenv("STAGING_TARGET_URL", "http://staging-target:8000")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_sent_equals_logged(tmp_path):
    log = Logger(tmp_path / "session.jsonl")
    log.start_session(data={"test": "seam3"})
    fail_next = {"on": False}
    received = []

    def log_event(type_, actor, data):
        if fail_next["on"] and type_ == "target_request":
            fail_next["on"] = False
            raise LogWriteError("simulated write failure")
        return log.log_event(type_, actor, data)

    budget_left = {"n": 6}

    def budget_check():
        budget_left["n"] -= 1
        return None if budget_left["n"] >= 0 else "session request limit"

    def target(request):
        received.append(request)
        return httpx.Response(500 if len(received) % 3 == 0 else 200, json={"reply": "r"})

    client = tc.TargetClient(budget_check, log_event, transport=httpx.MockTransport(target))
    start = tc.CLIENT_COUNTER
    for i in range(10):
        path = "//evil.example/x" if i in (2, 5) else "/chat"
        fail_next["on"] = i == 4
        client.send(TargetRequest(attempt_id=f"a-{i:04d}", path=path, body={"message": "m"}))

    records = list(iter_records(log.path))
    types = [r["type"] for r in records]
    sent = tc.CLIENT_COUNTER - start
    assert sent == len(received) == types.count("target_request") == types.count("target_response")
    assert sent > 0
    assert types.count("egress_denied") == 2 and "budget_stop" in types
    assert verify_chain(log.path) == len(records)
    # attempt_id is lifted to the top-level record field by the Logger
    assert {r["attempt_id"] for r in records if r["type"] == "target_request"} <= {f"a-{i:04d}" for i in range(10)}
