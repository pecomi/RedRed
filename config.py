"""Load secrets. Other code must not read os.environ directly; use get_settings() only.

- Local: reads .env at the repo root.
- Docker: environment variables passed via docker-compose env_file take precedence
  (.env is not copied into the image).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# If config.py moves into a subfolder such as src/, change this to parents[1].
_REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(_REPO_ROOT / ".env", override=False)


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is empty. Copy .env.example to .env and fill in the values.")
    return value


@dataclass(frozen=True)
class Settings:
    # repr=False: the key never shows up in print(settings), exception messages or logs.
    ku_llm_api_key: str = field(repr=False)
    ku_llm_base_url: str
    ku_llm_model: str
    # Empty until the staging target exists. The Target client must refuse to send while it is empty.
    staging_target_url: str
    # JSONL audit log. One file holds one session; start a new session with a new path.
    log_path: str = "logs/session.jsonl"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        ku_llm_api_key=_require("KU_LLM_API_KEY"),
        ku_llm_base_url=_require("KU_LLM_BASE_URL"),
        ku_llm_model=os.getenv("KU_LLM_MODEL", "claude-opus-5").strip(),
        staging_target_url=os.getenv("STAGING_TARGET_URL", "").strip(),
        log_path=os.getenv("LOG_PATH", "").strip() or "logs/session.jsonl",
    )
