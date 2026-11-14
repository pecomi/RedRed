import json

import httpx
import pytest

import config
from agent import budget as b
from agent.llm import LLMClient

FAKE_KEY = "sk-test-NEVER-LOG-ME"


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("KU_LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("KU_LLM_BASE_URL", "https://api.baze.co/v1/gateway/claude")
    monkeypatch.setenv("KU_LLM_MODEL", "claude-opus-5")
    monkeypatch.setattr(b, "PRICE_INPUT_USD_PER_MTOK", 10.0)
    monkeypatch.setattr(b, "PRICE_OUTPUT_USD_PER_MTOK", 50.0)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def ok_response(_request):
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            "stop_reason": "end_turn",
        },
    )


def make(handler, tmp_path):
    events, sent = [], []

    def transport_handler(request):
        sent.append((request, [e[0] for e in events]))  # what was logged at the moment of sending
        return handler(request)

    bud = b.Budget(log_event=lambda *e: events.append(e), kill_file=tmp_path / "KILL")
    client = LLMClient(bud, lambda *e: events.append(e), transport=httpx.MockTransport(transport_handler))
    return client, bud, events, sent


def test_request_goes_to_baze_messages_endpoint_with_key_header(tmp_path):
    client, bud, events, sent = make(ok_response, tmp_path)
    r = client.complete("planner", "sys", [{"role": "user", "content": "x"}], attempt_id="a-0001")
    req, logged_before_send = sent[0]
    assert str(req.url) == "https://api.baze.co/v1/gateway/claude/v1/messages"
    assert req.headers["x-api-key"] == FAKE_KEY
    assert json.loads(req.content)["model"] == "claude-opus-5"
    assert logged_before_send == ["model_request"]  # write-ahead
    assert r.text == "hi" and r.cost_usd == pytest.approx(1000 * 10 / 1e6 + 100 * 50 / 1e6)
    assert bud.cost == pytest.approx(r.cost_usd)


def test_key_never_appears_in_logs(tmp_path):
    client, bud, events, sent = make(ok_response, tmp_path)
    client.complete("verifier", "sys", [{"role": "user", "content": "x"}])
    assert FAKE_KEY not in json.dumps([e[2] for e in events], default=str)
    assert [e[0] for e in events] == ["model_request", "model_response"]


def test_http_error_is_logged_then_raised(tmp_path):
    client, bud, events, sent = make(lambda r: httpx.Response(401, json={"detail": "bad key"}), tmp_path)
    with pytest.raises(httpx.HTTPStatusError):
        client.complete("planner", "sys", [{"role": "user", "content": "x"}])
    assert events[-1][0] == "model_response" and events[-1][2]["status"] == 401


def test_no_prices_means_nothing_is_sent(tmp_path, monkeypatch):
    client, bud, events, sent = make(ok_response, tmp_path)
    monkeypatch.setattr(b, "PRICE_OUTPUT_USD_PER_MTOK", None)
    with pytest.raises(RuntimeError):
        client.complete("planner", "sys", [{"role": "user", "content": "x"}])
    assert sent == [] and events == []


def test_stopped_budget_means_nothing_is_sent(tmp_path):
    client, bud, events, sent = make(ok_response, tmp_path)
    bud.kill("test")
    with pytest.raises(b.BudgetStop):
        client.complete("planner", "sys", [{"role": "user", "content": "x"}])
    assert sent == []


def test_unknown_role_rejected(tmp_path):
    client, *_ = make(ok_response, tmp_path)
    with pytest.raises(ValueError):
        client.complete("attacker", "sys", [])
