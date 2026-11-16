"""Seam 3 (Logger <-> Target client): requests sent == target_request records logged.

Runs a mixed session (successes, blocked paths, budget stop, a log failure, HTTP errors)
against a mocked target, writes events to a JSONL file, then compares the file with
CLIENT_COUNTER and with what the mock target actually received.
"""
import json

import httpx
import pytest

import config
from agent import target_client as tc
from schemas.target_io import TargetRequest


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("KU_LLM_API_KEY", "sk-test-NEVER-LOG-ME")
    monkeypatch.setenv("KU_LLM_BASE_URL", "https://api.baze.co/v1/gateway/claude")
    monkeypatch.setenv("STAGING_TARGET_URL", "http://staging-target:8000")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def count_logged(path) -> int:
    with open(path, encoding="utf-8") as f:
        return sum(1 for line in f if json.loads(line)["type"] == "target_request")


def test_sent_equals_logged(tmp_path):
    log_path = tmp_path / "session.jsonl"
    fail_next = {"on": False}
    received = []

    def log_event(type_, actor, data):
        if fail_next["on"] and type_ == "target_request":
            fail_next["on"] = False
            return False
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": type_, "actor": actor, "data": data}) + "\n")
        return True

    budget_left = {"n": 6}

    def budget_check():
        budget_left["n"] -= 1
        return None if budget_left["n"] >= 0 else "session request limit"

    def target(request):
        received.append(request)
        return httpx.Response(500 if len(received) % 3 == 0 else 200, json={"reply": "r"})

    client = tc.TargetClient(log_event, budget_check, transport=httpx.MockTransport(target))
    start = tc.CLIENT_COUNTER
    for i in range(10):
        path = "//evil.example/x" if i in (2, 5) else "/chat"
        fail_next["on"] = i == 4
        client.send(TargetRequest(attempt_id=f"a-{i:04d}", path=path, body={"message": "m"}))

    sent = tc.CLIENT_COUNTER - start
    assert sent == len(received) == count_logged(log_path)
    assert sent > 0
