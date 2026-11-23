"""Kill switch through the whole session runner, offline (POSIX only: agent.logger needs fcntl).

The kill file appears while the Budget waits for the per-minute rate window. The session must
stop within one wait step, send nothing more, and report the kill as its stop reason.
"""
import functools

import httpx
import pytest

import config
from agent import budget as budget_mod
from agent.planner import RulePlanner

pytest.importorskip("fcntl", reason="agent.logger is POSIX-only; run in the container")
from agent import run_session as rs  # noqa: E402
from agent.logger import iter_records, verify_chain  # noqa: E402

SEEDS = [{"seed_id": f"advbench-{i:04d}", "goal": f"goal {i}", "target": "Sure"} for i in range(5)]


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("KU_LLM_API_KEY", "sk-test-NEVER-LOG-ME")
    monkeypatch.setenv("KU_LLM_BASE_URL", "https://api.baze.co/v1/gateway/claude")
    monkeypatch.setenv("STAGING_TARGET_URL", "http://staging-target:8000")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_kill_during_rate_wait_ends_session_with_kill_reason(tmp_path, monkeypatch):
    kill_file = tmp_path / "KILL"
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s
        if len(slept) == 2:
            kill_file.touch()

    monkeypatch.setattr(rs, "Budget", functools.partial(budget_mod.Budget, clock=lambda: now[0], sleep=sleep))
    received = []

    def handler(request):
        received.append(request)
        return httpx.Response(200, json={"reply": "I can't help with that."})

    n = budget_mod.MAX_TARGET_REQUESTS_PER_MIN
    r = rs.run_session(RulePlanner(SEEDS, [["plain"], ["korean_switch"], ["roleplay_wrapper"]], max_attempts=n + 3),
                       log_dir=tmp_path, kill_file=kill_file, transport=httpx.MockTransport(handler))

    assert r.stop_reason == "kill switch file present"
    assert r.sent == n and len(received) == n  # nothing after the kill file appeared
    assert now[0] <= 2 * budget_mod.WAIT_STEP_SECONDS + 1e-9
    records = list(iter_records(r.log_path))
    assert verify_chain(r.log_path) == len(records)
    types = [rec["type"] for rec in records]
    assert types.count("target_request") == n
    assert types[-3:] == ["kill", "budget_stop", "session_end"]
    assert records[-1]["data"]["stop_reason"] == "kill switch file present"
