import pytest

from agent import advbench


def test_load_verifies_hash_and_returns_seed_ids(tmp_path, monkeypatch):
    p = tmp_path / "hb.csv"
    p.write_text("goal,target\nG0,T0\nG1,T1\n", encoding="utf-8")
    monkeypatch.setattr(advbench, "SHA256", advbench.sha256_of(p))
    rows = advbench.load(p)
    assert rows[0] == {"seed_id": "advbench-0000", "goal": "G0", "target": "T0"}
    assert len(rows) == 2


def test_load_refuses_modified_file(tmp_path):
    p = tmp_path / "hb.csv"
    p.write_text("goal,target\ntampered,x\n", encoding="utf-8")
    with pytest.raises(advbench.IntegrityError):
        advbench.load(p)


def test_missing_file_says_how_to_fetch(tmp_path):
    with pytest.raises(FileNotFoundError, match="python -m agent.advbench"):
        advbench.load(tmp_path / "nope.csv")


@pytest.mark.live
def test_pinned_github_file_matches_sha256(tmp_path):
    p = advbench.fetch(tmp_path / "hb.csv")
    assert len(advbench.load(p)) == 520
