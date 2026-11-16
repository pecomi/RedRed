import httpx
import pytest

import config
from agent import target_client as tc
from agent.budget import Budget
from schemas.target_io import TargetRequest

TARGET = "http://staging-target:8000"


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("KU_LLM_API_KEY", "sk-test-NEVER-LOG-ME")
    monkeypatch.setenv("KU_LLM_BASE_URL", "https://api.baze.co/v1/gateway/claude")
    monkeypatch.setenv("STAGING_TARGET_URL", TARGET)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def make(handler=None, *, log_ok=True, budget=None):
    events, sent = [], []

    def log_event(type_, actor, data):
        if not log_ok:
            return False
        events.append(type_)
        return True

    def transport_handler(request):
        sent.append((request, list(events)))  # what was logged at the moment of sending
        return (handler or (lambda r: httpx.Response(200, json={"reply": "ok", "conversation_id": "c-1"})))(request)

    client = tc.TargetClient(
        log_event, budget or tc.stub_budget_check, transport=httpx.MockTransport(transport_handler)
    )
    return client, events, sent


def req(**kw):
    return TargetRequest(**{"attempt_id": "a-0001", "path": "/chat", "body": {"message": "hi"}, **kw})


def test_success_logs_request_before_sending_and_extracts_text():
    client, events, sent = make()
    before = tc.CLIENT_COUNTER
    resp = client.send(req())
    assert resp.error is None and resp.status == 200
    assert resp.text == "ok" and resp.conversation_id == "c-1"
    assert str(sent[0][0].url) == TARGET + "/chat"
    assert sent[0][1] == ["target_request"]
    assert events == ["target_request", "target_response"]
    assert tc.CLIENT_COUNTER == before + 1


@pytest.mark.parametrize(
    "path",
    ["//evil.example/x", "chat", "/a://b", "/x@evil.example", "/a b", "/a\nb", "/a\\b", "/a\x00b"],
)
def test_bad_paths_are_blocked_and_never_sent(path):
    client, events, sent = make()
    resp = client.send(req(path=path))
    assert resp.error == "blocked_path" and resp.status == 0
    assert events == ["egress_denied"] and sent == []


def test_disallowed_method_is_blocked():
    client, events, sent = make()
    assert client.send(req(method="DELETE")).error == "blocked_path"
    assert sent == []


def test_empty_target_url_refuses_to_send(monkeypatch):
    monkeypatch.setenv("STAGING_TARGET_URL", "")
    config.get_settings.cache_clear()
    client, events, sent = make()
    assert client.send(req()).error == "blocked_path"
    assert sent == []


def test_budget_exceeded_is_checked_after_path_and_before_logging():
    client, events, sent = make(budget=lambda: "session request limit")
    assert client.send(req()).error == "budget_exceeded"
    assert events == ["budget_stop"] and sent == []
    # A blocked path still wins over the budget check
    client, events, _ = make(budget=lambda: "session request limit")
    client.send(req(path="//evil.example/x"))
    assert events == ["egress_denied"]


def test_log_failure_means_no_send():
    client, _, sent = make(log_ok=False)
    before = tc.CLIENT_COUNTER
    assert client.send(req()).error == "log_failed"
    assert sent == [] and tc.CLIENT_COUNTER == before


def test_logger_exception_counts_as_log_failure():
    def boom(*_):
        raise OSError("disk full")

    client = tc.TargetClient(boom, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert client.send(req()).error == "log_failed"


def test_http_error_and_timeout_are_logged():
    client, events, _ = make(lambda r: httpx.Response(500, text="boom"))
    resp = client.send(req())
    assert resp.error == "http_error" and resp.status == 500 and resp.body_raw == "boom"
    assert events == ["target_request", "target_response"]

    def slow(r):
        raise httpx.ReadTimeout("slow", request=r)

    client, events, _ = make(slow)
    assert client.send(req()).error == "timeout"
    assert events == ["target_request", "target_response"]


def test_budget_adapter_converts_budget_stop(tmp_path):
    b = Budget(kill_file=tmp_path / "KILL")
    check = tc.budget_check_from(b)
    assert check() is None and b.target_requests == 1
    b.kill("test")
    assert check() == "test"
