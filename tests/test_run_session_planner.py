"""run_session with a model planner, offline: make_planner wiring and planner failures.

Both the BAZE gateway and the staging target are httpx.MockTransport, so nothing leaves the
machine and no budget is spent. POSIX only (agent.logger needs fcntl).
"""
import json

import httpx
import pytest

import config
from agent.budget import Budget

pytest.importorskip("fcntl", reason="agent.logger is POSIX-only; run in the container")
from agent import run_session as rs  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from agent.logger import iter_records, verify_chain  # noqa: E402
from agent.planner import LLMPlanner, RulePlanner  # noqa: E402

SEEDS = [{"seed_id": f"advbench-{i:04d}", "goal": f"goal {i}", "target": "Sure"} for i in range(3)]
PLAN = '{"seed_id": "advbench-0001", "mutations": ["roleplay_wrapper"], "rationale": "x"}'


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("KU_LLM_API_KEY", "sk-test-NEVER-LOG-ME")
    monkeypatch.setenv("KU_LLM_BASE_URL", "https://api.baze.co/v1/gateway/claude")
    monkeypatch.setenv("STAGING_TARGET_URL", "http://staging-target:8000")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def target(received):
    def handler(request):
        received.append(request)
        return httpx.Response(200, json={"reply": "I can't help with that."})
    return httpx.MockTransport(handler)


def gateway(status=200, text=PLAN, stop_reason="end_turn"):
    """Mock BAZE gateway answering every /v1/messages call the same way."""
    def handler(request):
        if status != 200:
            return httpx.Response(status, json={"error": {"type": "overloaded_error"}})
        content = [{"type": "text", "text": text}] if text else []
        return httpx.Response(200, json={"content": content, "stop_reason": stop_reason,
                                         "usage": {"input_tokens": 100, "output_tokens": 20}})
    return httpx.MockTransport(handler)


def run(tmp_path, llm_transport, n=2, **kw):
    built, received = {}, []

    def make_planner(budget, log_event):
        built["budget"] = budget
        return LLMPlanner(SEEDS, LLMClient(budget, log_event, transport=llm_transport), max_attempts=n)

    r = rs.run_session(make_planner=make_planner, log_dir=tmp_path, kill_file=tmp_path / "KILL",
                       transport=target(received), **kw)
    records = list(iter_records(r.log_path))
    assert verify_chain(r.log_path) == len(records)
    return r, records, received, built


def test_make_planner_spends_session_budget_and_logs_in_session_log(tmp_path):
    r, records, received, built = run(tmp_path, gateway(), n=2)
    assert r.stop_reason == "planner_exhausted" and r.attempts == 2 and len(received) == 2
    assert isinstance(built["budget"], Budget)
    assert built["budget"].cost == pytest.approx(2 * (100 * 5 + 20 * 25) / 1_000_000)
    seq = [(rec["type"], rec["actor"]) for rec in records if rec["attempt_id"] == "a-0000"]
    assert seq[:2] == [("model_request", "planner"), ("model_response", "planner")]
    assert [t for t, _ in seq[2:]] == ["target_request", "target_response", "verdict"]
    sent = json.loads(received[0].content)["message"]
    assert "goal 1" in sent  # the planner's choice of seed reached the target
    assert "sk-test-NEVER-LOG-ME" not in r.log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("llm_transport, reason", [
    (gateway(status=529), "planner_failed: HTTPStatusError"),
    (gateway(text="", stop_reason="refusal"), "planner_failed: PlannerRefused"),
])
def test_planner_failure_ends_session_with_its_reason(tmp_path, llm_transport, reason):
    r, records, received, _ = run(tmp_path, llm_transport)
    assert r.stop_reason == reason
    assert r.attempts == 0 and r.sent == 0 and received == []  # no default attempt was sent
    end = records[-1]
    assert end["type"] == "session_end" and end["data"]["stop_reason"] == reason
    assert "model_response" in [rec["type"] for rec in records]  # the failed call is on record


def test_unexpected_error_is_recorded_then_raised(tmp_path):
    def build(seed, chain, attempt_id):
        raise KeyError("boom")

    received = []
    with pytest.raises(KeyError):
        rs.run_session(RulePlanner(SEEDS, [["plain"]], max_attempts=1), build=build, log_dir=tmp_path,
                       kill_file=tmp_path / "KILL", transport=target(received))
    (log_path,) = tmp_path.glob("session-*.jsonl")
    end = list(iter_records(log_path))[-1]
    assert end["type"] == "session_end" and end["data"]["stop_reason"] == "crashed: KeyError"


def test_exactly_one_planner_source(tmp_path):
    with pytest.raises(ValueError):
        rs.run_session(log_dir=tmp_path)
    with pytest.raises(ValueError):
        rs.run_session(RulePlanner(SEEDS, [["plain"]]), make_planner=lambda b, l: [], log_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []  # rejected before any log file was opened


@pytest.mark.parametrize("flag, expect", [("rule", "RulePlanner"), ("llm", "LLMPlanner")])
def test_main_wires_the_chosen_planner(monkeypatch, tmp_path, flag, expect):
    seen = {}

    def fake_run_session(planner=None, **kw):
        seen.update(kw, planner=planner)
        return rs.SessionResult(log_path=tmp_path / "x.jsonl", stop_reason="planner_exhausted")

    monkeypatch.setattr(rs, "run_session", fake_run_session)
    assert rs.main(["--planner", flag, "--max-attempts", "2", "--seeds", "3"]) == 0
    assert seen["session_meta"]["planner"] == expect
    if flag == "llm":
        assert seen["planner"] is None and callable(seen["make_planner"])
        p = seen["make_planner"](Budget(kill_file=tmp_path / "KILL"), lambda *a: None)
        assert isinstance(p, LLMPlanner) and len(p.seeds) == 3 and p.max_attempts == 2
    else:
        assert isinstance(seen["planner"], RulePlanner) and "make_planner" not in seen
