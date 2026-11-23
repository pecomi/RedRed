import pytest

from agent import budget as b


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(tmp_path):
    ft, events = FakeTime(), []
    bud = b.Budget(
        log_event=lambda *e: events.append(e), clock=ft.clock, sleep=ft.sleep, kill_file=tmp_path / "KILL"
    )
    return bud, ft, events


def test_per_minute_rate_waits_instead_of_exceeding(tmp_path):
    bud, ft, events = make(tmp_path)
    for _ in range(b.MAX_TARGET_REQUESTS_PER_MIN):
        bud.acquire_target_request()
    assert ft.t == 0
    bud.acquire_target_request()  # 11th request in the same minute: must wait
    assert ft.t >= 60
    assert events == []


def test_session_request_limit_stops_and_logs_once(tmp_path):
    bud, ft, events = make(tmp_path)
    for _ in range(b.MAX_TARGET_REQUESTS_PER_SESSION):
        bud.acquire_target_request()
        ft.t += 7  # 7 s apart stays under 10/min; 200 x 7 s = 1400 s, still under 30 min
    with pytest.raises(b.BudgetStop, match="request limit"):
        bud.acquire_target_request()
    with pytest.raises(b.BudgetStop):
        bud.check()
    assert [e[0] for e in events] == ["budget_stop"]


def test_time_limit(tmp_path):
    bud, ft, events = make(tmp_path)
    ft.t = b.MAX_SESSION_SECONDS
    with pytest.raises(b.BudgetStop, match="time limit"):
        bud.check()


def test_cost_limit(tmp_path):
    bud, ft, events = make(tmp_path)
    bud.add_cost(b.MAX_SESSION_COST_USD)
    with pytest.raises(b.BudgetStop, match="cost limit"):
        bud.check()
    assert events[0][0] == "budget_stop"


def test_kill_file_stops_session(tmp_path):
    bud, ft, events = make(tmp_path)
    (tmp_path / "KILL").touch()
    with pytest.raises(b.BudgetStop):
        bud.acquire_target_request()
    assert events[0][0] == "kill"
    assert bud.target_requests == 0


def test_kill_method_then_check_raises(tmp_path):
    bud, ft, events = make(tmp_path)
    bud.kill("supervisor pressed stop")
    with pytest.raises(b.BudgetStop, match="supervisor"):
        bud.check()
    assert [e[0] for e in events] == ["kill"]


def test_cost_refuses_without_prices(monkeypatch):
    monkeypatch.setattr(b, "PRICE_INPUT_USD_PER_MTOK", None)
    with pytest.raises(RuntimeError):
        b.cost_usd(10, 10)


def test_kill_during_rate_wait_stops_within_one_step(tmp_path):
    """The kill file is checked while waiting for the rate window, not only after the wait."""
    bud, ft, events = make(tmp_path)
    for _ in range(b.MAX_TARGET_REQUESTS_PER_MIN):
        bud.acquire_target_request()
    steps = []

    def sleep(s):
        steps.append(s)
        ft.t += s
        if len(steps) == 3:  # the supervisor creates the kill file ~3 s into the wait
            (tmp_path / "KILL").touch()

    bud.sleep = sleep
    with pytest.raises(b.BudgetStop, match="kill switch"):
        bud.acquire_target_request()
    assert all(s <= b.WAIT_STEP_SECONDS for s in steps)
    assert ft.t <= 3 * b.WAIT_STEP_SECONDS + 1e-9  # stopped right after the step that saw the file
    assert [e[0] for e in events] == ["kill"]
    assert bud.target_requests == b.MAX_TARGET_REQUESTS_PER_MIN  # the 11th request was not counted


def test_rate_wait_is_split_into_short_steps(tmp_path):
    bud, ft, events = make(tmp_path)
    for _ in range(b.MAX_TARGET_REQUESTS_PER_MIN):
        bud.acquire_target_request()
    steps = []
    bud.sleep = lambda s: (steps.append(s), setattr(ft, "t", ft.t + s))
    bud.acquire_target_request()
    assert ft.t >= 60 and len(steps) >= 60
    assert all(s <= b.WAIT_STEP_SECONDS for s in steps)
