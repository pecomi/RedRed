import json

import pytest

from scripts import count_egress as ce

try:
    from agent.logger import Logger
except ImportError:  # agent.logger needs fcntl (POSIX); on Windows write plain lines instead
    Logger = None

SQUID = """\
1763270000.001     12 172.20.0.3 TCP_TUNNEL/200 5120 CONNECT api.baze.co:443 - HIER_DIRECT/1.2.3.4 -
1763270001.002      0 172.20.0.3 TCP_DENIED/403 3900 CONNECT example.com:443 - HIER_NONE/- text/html
1763270002.003      0 172.20.0.3 TCP_DENIED/403 3900 CONNECT evil.example:443 - HIER_NONE/- text/html
1763270003.004     40 172.20.0.3 TCP_MISS/200 210 POST http://staging-target:8000/chat - HIER_DIRECT/172.20.0.4 application/json
"""


def write(tmp_path, squid=SQUID, events=("target_request", "target_response", "egress_denied")):
    (tmp_path / "access.log").write_text(squid)
    agent_log = tmp_path / "s.jsonl"
    if Logger:
        log = Logger(agent_log)
        log.start_session()
        for t in events:
            log.log_event(t, "target_client", {"attempt_id": "a-0001"})
    else:
        agent_log.write_text("".join(json.dumps({"type": t}) + "\n" for t in events))
    return ["--squid", str(tmp_path / "access.log"), "--agent", str(agent_log), "--json"]


def test_clean_logs_report_zero_reached(tmp_path, capsys):
    assert ce.main(write(tmp_path)) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["reached_non_target"] == 0 and r["squid_denied"] == 2
    assert r["egress_denied"] == 1 and r["sent_eq_logged"] and r["squid_target"] == 1
    assert r["chain_ok"] is (True if Logger else None)


def test_non_denied_entry_to_other_host_counts_as_reached(tmp_path, capsys):
    leak = SQUID + "1763270004.005 30 172.20.0.3 TCP_MISS/503 0 GET http://other.example/ - HIER_NONE/- -\n"
    assert ce.main(write(tmp_path, squid=leak)) == 1
    assert json.loads(capsys.readouterr().out)["reached_hosts"] == ["other.example"]


def test_unanswered_request_fails_sent_eq_logged(tmp_path, capsys):
    assert ce.main(write(tmp_path, events=("target_request",))) == 1
    assert json.loads(capsys.readouterr().out)["sent_eq_logged"] is False


@pytest.mark.skipif(Logger is None, reason="agent.logger is POSIX-only; runs in the container")
def test_tampered_log_breaks_chain(tmp_path, capsys):
    args = write(tmp_path)
    agent_log = tmp_path / "s.jsonl"
    lines = agent_log.read_text().splitlines(keepends=True)
    del lines[1]  # drop one record, e.g. a target_request someone wanted to hide
    agent_log.write_text("".join(lines))
    assert ce.main(args) == 1
    assert json.loads(capsys.readouterr().out)["chain_ok"] is False


def test_mismatches_in_two_sessions_cannot_cancel_out(tmp_path, capsys):
    # Session A: a response with no request; session B: a request with no response.
    # The totals match (1 == 1), but each session is wrong on its own.
    args = write(tmp_path, events=("target_response",))
    (tmp_path / "a").mkdir()
    a_args = write(tmp_path / "a", events=("target_request",))
    args[args.index("--agent") + 1 : args.index("--agent") + 2] = [str(tmp_path / "s.jsonl"), a_args[3]]
    assert ce.main(args) == 1
    r = json.loads(capsys.readouterr().out)
    assert r["target_request"] == r["target_response"] == 1
    assert r["sent_eq_logged"] is False and len(r["sessions_mismatched"]) == 2
