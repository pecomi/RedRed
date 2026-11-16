from schemas.target_io import TargetRequest, TargetResponse


def test_request_has_no_host_field_and_logs_round_trip():
    r = TargetRequest(attempt_id="a-0001", path="/chat", body={"message": "hi"}, seed_id="advbench-0000")
    d = r.to_log()
    assert d["path"] == "/chat" and "host" not in d and "url" not in d
    assert d["method"] == "POST" and d["mutations"] == []


def test_response_body_raw_is_separate_from_text():
    resp = TargetResponse(attempt_id="a-0001", turn=1, status=200, text="clean", body_raw='{"reply":"clean"}')
    d = resp.to_log()
    assert d["text"] == "clean" and d["body_raw"] == '{"reply":"clean"}' and d["error"] is None
