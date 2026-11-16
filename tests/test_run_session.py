"""Session runner, offline: mocked target, real Logger and Budget (POSIX only: agent.logger needs fcntl)."""
import httpx
import pytest

import config
from agent import budget as budget_mod
from agent.generator import build_request
from agent.planner import RulePlanner
from schemas.verifier_input import VerifierOutput

pytest.importorskip("fcntl", reason="agent.logger is POSIX-only; run in the container")
from agent.logger import iter_records, verify_chain  # noqa: E402
from agent.run_session import run_session  # noqa: E402

SEEDS = [{"seed_id": f"advbench-{i:04d}", "goal": f"goal {i}", "target": "Sure"} for i in range(5)]


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


def run(tmp_path, n=4, **kw):
    received = []
    kw.setdefault("transport", target(received))
    r = run_session(
        RulePlanner(SEEDS, [["plain"]], max_attempts=n),
        log_dir=tmp_path, kill_file=tmp_path / "KILL", **kw,
    )
    records = list(iter_records(r.log_path))
    assert verify_chain(r.log_path) == len(records)
    return r, records, received


def types(records):
    return [rec["type"] for rec in records]


def test_full_session_logs_every_step_and_sent_equals_logged(tmp_path):
    r, records, received = run(tmp_path, n=4)
    t = types(records)
    assert t[0] == "session_start" and t[-1] == "session_end"
    assert r.stop_reason == "planner_exhausted" and r.attempts == 4
    assert r.sent == len(received) == t.count("target_request") == t.count("target_response") == 4
    assert t.count("verdict") == 4 and r.verdicts == {"uncertain": 4}  # stub verifier never claims a finding
    # every request is logged before its response, and the verdict comes after both
    for attempt in ("a-0000", "a-0003"):
        seq = [rec["type"] for rec in records if rec["attempt_id"] == attempt]
        assert seq == ["target_request", "target_response", "verdict"]


def test_kill_switch_mid_session_stops_with_zero_further_requests(tmp_path):
    kill_file = tmp_path / "KILL"

    def verifier(vin):
        if vin.attempt_id == "a-0001":
            kill_file.write_text("supervisor")  # supervisor stops the session here
        return VerifierOutput(attempt_id=vin.attempt_id, verdict="not_finding", basis="code")

    r, records, received = run(tmp_path, n=5, verifier=verifier)
    t = types(records)
    assert "kill" in t and r.stop_reason == "kill switch file present"
    assert len(received) == 2 and r.sent == 2
    assert "target_request" not in t[t.index("kill"):]
    assert t[-1] == "session_end"


def test_budget_limit_stops_session(tmp_path, monkeypatch):
    monkeypatch.setattr(budget_mod, "MAX_TARGET_REQUESTS_PER_SESSION", 2)
    r, records, received = run(tmp_path, n=5)
    assert r.stop_reason == "budget_exceeded" and r.sent == len(received) == 2
    assert "budget_stop" in types(records)


def test_blocked_path_is_recorded_and_session_continues(tmp_path):
    def build(seed, chain, attempt_id):
        req = build_request(seed, chain, attempt_id)
        if attempt_id == "a-0001":
            return type(req)(**{**req.__dict__, "path": "//evil.example/x"})
        return req

    r, records, received = run(tmp_path, n=3, build=build)
    assert r.errors == {"blocked_path": 1} and r.sent == len(received) == 2
    assert types(records).count("egress_denied") == 1 and r.stop_reason == "planner_exhausted"


def test_log_failure_stops_session_before_sending(tmp_path):
    def verifier(vin):
        r_path.chmod(0o444)  # the log becomes unwritable after the first attempt
        return VerifierOutput(attempt_id=vin.attempt_id, verdict="not_finding", basis="code")

    received = []
    r_path = None

    import agent.run_session as rs

    original = rs.new_log_path

    def capture(log_dir):
        nonlocal r_path
        r_path = original(log_dir)
        return r_path

    rs.new_log_path = capture
    try:
        r = run_session(
            RulePlanner(SEEDS, [["plain"]], max_attempts=4),
            verifier=verifier, transport=target(received), log_dir=tmp_path, kill_file=tmp_path / "KILL",
        )
    finally:
        rs.new_log_path = original
        r_path.chmod(0o644)
    assert r.stop_reason.startswith("log_failed")
    assert len(received) == 1  # nothing was sent once the log could not be written


def test_make_verifier_gets_this_sessions_budget_and_logger(tmp_path):
    """LLM injection point: a judge built by make_verifier spends from the session Budget and
    writes model_request / model_response into the same session log, before the verdict."""
    from types import SimpleNamespace

    from agent.budget import Budget
    from agent.verifier import Verifier

    built = {}

    class FakeJudge:
        def __init__(self, budget, log_event):
            self.budget, self.log_event = budget, log_event

        def complete(self, role, system, messages, *, attempt_id=None, max_tokens=1024, temperature=None):
            self.budget.check()
            self.log_event("model_request", role, {"attempt_id": attempt_id})
            self.log_event("model_response", role, {"attempt_id": attempt_id})
            return SimpleNamespace(text='{"verdict": "not_finding", "evidence_quote": "", "rationale": "refused"}')

    def make_verifier(budget, log_event):
        built["budget"] = budget
        return Verifier(llm=FakeJudge(budget, log_event))

    # a reply long enough that the code checks are inconclusive, so the judge is asked
    def long_reply(received):
        def handler(request):
            received.append(request)
            return httpx.Response(200, json={"reply": "Here is a long and neutral answer. " * 10})
        return httpx.MockTransport(handler)

    received = []
    r, records, _ = run(tmp_path, n=2, make_verifier=make_verifier, transport=long_reply(received))
    assert isinstance(built["budget"], Budget)
    assert r.verdicts == {"not_finding": 2}
    seq = [rec["type"] for rec in records if rec["attempt_id"] == "a-0000"]
    assert seq == ["target_request", "target_response", "model_request", "model_response", "verdict"]
