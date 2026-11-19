"""run_session drives RulePlanner (unchanged) and feeds verdicts to LLMPlanner.

Offline, following tests/test_run_session.py: the `settings` fixture sets the env the Target
client reads, and a mock httpx transport returns 200, so no staging target needs to run.
This isolates the feedback wiring, not the network.
"""
from dataclasses import dataclass

import httpx
import pytest

import config
from agent.run_session import run_session

pytest.importorskip("fcntl", reason="agent.logger is POSIX-only; run in the container")
from agent.planner import RulePlanner, LLMPlanner  # noqa: E402

SEEDS = [{"seed_id": f"advbench-{i:04d}", "goal": f"g{i}", "target": "Sure"} for i in range(4)]


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("KU_LLM_API_KEY", "sk-test-NEVER-LOG-ME")
    monkeypatch.setenv("KU_LLM_BASE_URL", "https://api.baze.co/v1/gateway/claude")
    monkeypatch.setenv("STAGING_TARGET_URL", "http://staging-target:8000")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def ok_transport():
    """Every POST /chat returns 200 with a benign reply, with no network."""
    return httpx.MockTransport(
        lambda request: httpx.Response(200, json={"reply": "I can't help with that.", "conversation_id": None})
    )


def test_ruleplanner_still_runs_every_attempt(tmp_path):
    p = RulePlanner(SEEDS, [["plain"], ["korean_switch"]], max_attempts=6)
    r = run_session(p, log_dir=tmp_path, kill_file=tmp_path / "KILL", transport=ok_transport())
    assert r.attempts == 6 and r.stop_reason == "planner_exhausted"


@dataclass
class FakeResult:
    text: str


class FakeLLM:
    def __init__(self):
        self.histories = []

    def complete(self, role, system, messages, **kw):
        import json
        self.histories.append(json.loads(messages[0]["content"])["history"])
        return FakeResult('{"seed_id":"advbench-0000","mutations":["plain"]}')


def test_llmplanner_gets_each_verdict_through_run_session(tmp_path):
    llm = FakeLLM()
    p = LLMPlanner(SEEDS, llm, max_attempts=3)
    r = run_session(p, log_dir=tmp_path, kill_file=tmp_path / "KILL", transport=ok_transport())
    assert r.attempts == 3
    assert not r.errors, f"requests were blocked: {dict(r.errors)}"
    # 1st plan: empty history. 2nd carries 1 prior verdict, 3rd carries 2 — fed back by the runner.
    assert llm.histories[0] == []
    assert len(llm.histories[1]) == 1
    assert len(llm.histories[2]) == 2
    assert llm.histories[1][0]["verdict"] in ("not_finding", "uncertain", "finding")
