"""Environment configuration for the front door (the only env-specific module).

Reads come with a board token; writes use the operator's own token (OAuth in
prod; FRONT_DOOR_DEV_TOKEN for local dev). See README auth section.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Config:
    read_token: str
    repos: tuple = field(default_factory=tuple)
    dev_operator_token: str = ""
    approve_webapp_url: str = ""
    approve_signing_secret: str = ""
    api_base: str = "https://api.github.com"
    host: str = "127.0.0.1"
    port: int = 8000


def _split_repos(raw: str) -> tuple:
    return tuple(r.strip() for r in (raw or "").split(",") if r.strip())


def load(env: Optional[dict] = None) -> Config:
    e = env if env is not None else os.environ
    return Config(
        read_token=(e.get("GITHUB_READ_TOKEN") or "").strip(),
        repos=_split_repos(e.get("FRONT_DOOR_REPOS", "")),
        dev_operator_token=(e.get("FRONT_DOOR_DEV_TOKEN") or "").strip(),
        approve_webapp_url=(e.get("APPROVE_WEBAPP_URL") or "").strip(),
        approve_signing_secret=(e.get("APPROVE_SIGNING_SECRET") or "").strip(),
        api_base=(e.get("GITHUB_API_BASE") or "https://api.github.com").strip(),
        host=(e.get("FRONT_DOOR_HOST") or "127.0.0.1").strip(),
        port=int(e.get("FRONT_DOOR_PORT") or "8000"),
    )


__all__ = ["Config", "load"]
