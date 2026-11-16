"""Seam 1 (agent <-> egress).

(A)  send() with a host-smuggling path   -> egress_denied, nothing sent   [offline, here]
(B)  non-allowed host through Squid      -> TCP_DENIED in Squid access.log [live, tests/seam1_egress.sh]
(B') direct connection bypassing Squid   -> no route, connection fails    [live, tests/seam1_egress.sh]
"""
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

import config
from agent import target_client as tc
from schemas.target_io import TargetRequest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("KU_LLM_API_KEY", "sk-test-NEVER-LOG-ME")
    monkeypatch.setenv("KU_LLM_BASE_URL", "https://api.baze.co/v1/gateway/claude")
    monkeypatch.setenv("STAGING_TARGET_URL", "http://staging-target:8000")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.mark.parametrize("path", ["//evil.example/x", "/x@evil.example", "/https://evil.example"])
def test_a_host_smuggling_path_is_denied_and_logged(settings, path):
    events, sent = [], []
    client = tc.TargetClient(
        lambda t, a, d: events.append((t, d)) or True,
        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)),
    )
    resp = client.send(TargetRequest(attempt_id="a-0001", path=path))
    assert resp.error == "blocked_path"
    assert sent == []
    assert [t for t, _ in events] == ["egress_denied"] and events[0][1]["request"]["path"] == path


@pytest.mark.live
def test_b_and_b_prime_in_sandbox():
    """Needs `docker compose up -d`. A human must confirm before running (pytest -m live)."""
    bash = shutil.which("bash")
    if not bash or not shutil.which("docker"):
        pytest.skip("bash and docker are required")
    result = subprocess.run([bash, "tests/seam1_egress.sh"], cwd=REPO, capture_output=True, text=True)
    print(result.stdout, result.stderr)
    assert result.returncode == 0
