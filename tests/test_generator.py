from agent.generator import generate, build_request, TARGET_PATH
from schemas.target_io import TargetRequest

SEEDS = [{"seed_id": "advbench-0000", "goal": "do X", "target": "Sure"},
         {"seed_id": "advbench-0001", "goal": "do Y", "target": "Sure"}]


def test_build_request_records_seed_and_mutations():
    r = build_request(SEEDS[0], ["roleplay_wrapper"], "a-0001")
    assert isinstance(r, TargetRequest)
    assert r.path == TARGET_PATH and r.seed_id == "advbench-0000"
    assert r.mutations == ["roleplay_wrapper"] and r.body["message"] != "do X"  # mutated


def test_generate_crosses_seeds_with_chains_and_ids_are_sequential():
    chains = [["plain"], ["korean_switch"]]
    out = list(generate(SEEDS, chains))
    assert len(out) == 4  # 2 seeds x 2 chains
    assert [r.attempt_id for r in out] == ["a-0000", "a-0001", "a-0002", "a-0003"]


def test_generate_limit_caps_output():
    out = list(generate(SEEDS, [["plain"], ["korean_switch"]], limit=3))
    assert len(out) == 3
