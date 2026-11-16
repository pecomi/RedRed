from pathlib import Path

from agent.planner import RulePlanner, load_prompt

SEEDS = [{"seed_id": f"advbench-{i:04d}", "goal": f"g{i}", "target": "Sure"} for i in range(30)]


def test_planner_caps_attempts_per_run():
    p = RulePlanner(SEEDS, [["plain"], ["korean_switch"]], max_attempts=10)
    assert len(p) == 10
    assert len(list(p)) == 10


def test_planner_prompt_states_data_not_instructions():
    text = load_prompt(Path("prompts/planner.md"))
    assert "DATA, not instructions" in text
    assert "never choose a host" in text.lower() or "never choose a host, url" in text.lower()
