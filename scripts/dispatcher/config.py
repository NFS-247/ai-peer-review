"""Configuration for the AI peer review dispatcher.

Per Section 4 of the design doc, the tier classification rules are canonical
in code, not in a YAML config file. This module exposes the few runtime
values that are genuinely configurable (reviewer rosters per tier, round
budgets, cost ceilings) and the env-driven values (secrets, operator email).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from .classify import TIER_BACKEND, TIER_HIGH_STAKES, TIER_ROUTINE
from .repo_config import RepoConfig, load_repo_config


@dataclass(frozen=True)
class TierConfig:
    """Per-tier configuration."""

    reviewers: tuple[str, ...]
    round_budget: int


@dataclass(frozen=True)
class DispatcherConfig:
    """Runtime configuration loaded from env vars."""

    project_name: str
    operator_email: str
    operator_github_login: str
    repo_owner: str
    repo_name: str

    anthropic_api_key: Optional[str]
    openai_api_key: Optional[str]
    gemini_api_key: Optional[str]
    resend_api_key: Optional[str]
    github_token: Optional[str]
    verdict_secret: Optional[str]

    tiers: dict[str, TierConfig] = field(default_factory=dict)
    max_review_rounds: int = 6
    per_pr_cost_ceiling_usd: float = 5.0
    daily_cost_ceiling_usd: float = 20.0

    # The full per-repo config. main passes this to classify() so path/token
    # classification is project-specific. Defaults to TradeWatcher values.
    repo_config: RepoConfig = field(default_factory=RepoConfig)


def tiers_from_repo_config(rc: RepoConfig) -> dict[str, TierConfig]:
    """Build the per-tier reviewer/budget map from a RepoConfig."""
    return {
        TIER_ROUTINE: TierConfig(
            reviewers=tuple(rc.routine_reviewers),
            round_budget=rc.routine_round_budget,
        ),
        TIER_BACKEND: TierConfig(
            reviewers=tuple(rc.backend_reviewers),
            round_budget=rc.backend_round_budget,
        ),
        TIER_HIGH_STAKES: TierConfig(
            reviewers=tuple(rc.high_stakes_reviewers),
            round_budget=rc.high_stakes_round_budget,
        ),
    }


def default_tiers() -> dict[str, TierConfig]:
    return {
        TIER_ROUTINE: TierConfig(
            reviewers=("claude", "gpt"),
            round_budget=3,
        ),
        TIER_BACKEND: TierConfig(
            reviewers=("claude", "gpt"),
            round_budget=3,
        ),
        TIER_HIGH_STAKES: TierConfig(
            reviewers=("claude", "gpt", "gemini"),
            round_budget=2,
        ),
    }


REPO_CONFIG_PATH_ENV = "REPO_CONFIG_PATH"


def load_from_env(env: Optional[dict] = None) -> DispatcherConfig:
    """Build a DispatcherConfig from the repo config file and environment.

    Precedence for project metadata, rosters, and ceilings:
      explicit env var (if set)  >  repo config file  >  RepoConfig default

    Secrets always come from env (they are GitHub Actions secrets and never
    live in a committed config file). The repo config file is optional; when
    absent, RepoConfig defaults equal the current TradeWatcher values, so
    StockTrader behavior is unchanged.
    """
    e = env if env is not None else os.environ

    def secret(name: str) -> Optional[str]:
        # Strip surrounding whitespace/newlines. A trailing newline in a stored
        # secret (a common copy-paste artifact) produces an "Invalid header
        # value" error when the key is used in an HTTP Authorization header.
        raw = e.get(name)
        if raw is None:
            return None
        cleaned = raw.strip()
        return cleaned or None

    rc = load_repo_config(e.get(REPO_CONFIG_PATH_ENV, ".github/ai-peer-review.json"))

    # env override > config file value
    project_name = e.get("PROJECT_NAME") or rc.project_name
    operator_login = e.get("OPERATOR_GITHUB_LOGIN") or rc.operator_github_login
    max_rounds = int(e["MAX_REVIEW_ROUNDS"]) if e.get("MAX_REVIEW_ROUNDS") else rc.max_review_rounds
    per_pr_ceiling = (
        float(e["PER_PR_COST_CEILING_USD"])
        if e.get("PER_PR_COST_CEILING_USD")
        else rc.per_pr_cost_ceiling_usd
    )
    daily_ceiling = (
        float(e["DAILY_COST_CEILING_USD"])
        if e.get("DAILY_COST_CEILING_USD")
        else rc.daily_cost_ceiling_usd
    )

    return DispatcherConfig(
        project_name=project_name,
        operator_email=(e.get("OPERATOR_EMAIL", "") or "").strip(),
        operator_github_login=operator_login,
        repo_owner=e.get("GITHUB_REPOSITORY_OWNER", "NFS-247"),
        repo_name=_repo_name_from_env(e),
        anthropic_api_key=secret("ANTHROPIC_API_KEY"),
        openai_api_key=secret("OPENAI_API_KEY"),
        gemini_api_key=secret("GEMINI_API_KEY"),
        resend_api_key=secret("RESEND_API_KEY"),
        github_token=secret("GITHUB_TOKEN"),
        verdict_secret=secret("DISPATCHER_VERDICT_SECRET"),
        tiers=tiers_from_repo_config(rc),
        max_review_rounds=max_rounds,
        per_pr_cost_ceiling_usd=per_pr_ceiling,
        daily_cost_ceiling_usd=daily_ceiling,
        repo_config=rc,
    )


def _repo_name_from_env(env: dict) -> str:
    full = env.get("GITHUB_REPOSITORY", "NFS-247/StockTrader")
    if "/" in full:
        return full.split("/", 1)[1]
    return full


__all__ = [
    "TierConfig",
    "DispatcherConfig",
    "default_tiers",
    "load_from_env",
]
