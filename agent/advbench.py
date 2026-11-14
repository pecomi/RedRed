"""AdvBench loader, pinned to a commit and a sha256 (Proposal §3, ML-BOM).

fetch() downloads from GitHub. Run it once on the host, not inside the sandbox
(the sandbox egress allows only the model API and the target). load() never uses the network.
"""
from __future__ import annotations

import csv
import hashlib
import tempfile
import urllib.request
from pathlib import Path

COMMIT = "098262edf85f807224e70ecd87b9d83716bf6b73"
SHA256 = "6cd1a5c63c07610d7eb67307772ee5606017ee950b5770ab288a2c487489d3e1"
URL = f"https://raw.githubusercontent.com/llm-attacks/llm-attacks/{COMMIT}/data/advbench/harmful_behaviors.csv"
DEFAULT_PATH = Path("data/advbench/harmful_behaviors.csv")


class IntegrityError(Exception):
    """The file on disk is not the pinned AdvBench file. Do not run."""


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(path: Path = DEFAULT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        with urllib.request.urlopen(URL, timeout=30) as r:
            tmp.write(r.read())
        tmp_path = Path(tmp.name)
    got = sha256_of(tmp_path)
    if got != SHA256:
        tmp_path.unlink()
        raise IntegrityError(f"downloaded sha256 {got} != pinned {SHA256}")
    tmp_path.replace(path)
    return path


def load(path: Path = DEFAULT_PATH) -> list[dict]:
    """Verify the pinned sha256, then return rows as {seed_id, goal, target}."""
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run: python -m agent.advbench")
    got = sha256_of(path)
    if got != SHA256:
        raise IntegrityError(f"{path} sha256 {got} != pinned {SHA256}")
    with open(path, newline="", encoding="utf-8") as f:
        return [
            {"seed_id": f"advbench-{i:04d}", "goal": row["goal"], "target": row["target"]}
            for i, row in enumerate(csv.DictReader(f))
        ]


if __name__ == "__main__":
    p = fetch()
    print(f"saved {p} ({len(load(p))} rows, sha256 verified)")
