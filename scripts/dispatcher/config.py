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
from .repo_config import DEFAULT_CONFIG_PATH, RepoConfig, resolve_repo_config


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
    google_chat_webhook_url: Optional[str] = None
    approve_webapp_url: Optional[str] = None
    approve_signing_secret: Optional[str] = None
    # Verified Resend sender for escalation emails (see repo_config.email_from).
    # Empty -> the default onboarding@resend.dev, which can't deliver to the
    # operator, so email fails and the PR-comment fallback carries the alert.
    email_from: str = ""
    # GitHub App credentials (optional). When BOTH are set, the dispatcher mints a
    # per-installation token (its own 5–15k req/hr quota) instead of the per-repo
    # GITHUB_TOKEN's 1k/hr — see github_app.py / SCALING.md Move 1.
    github_app_id: str = ""
    github_app_private_key: str = ""

    tiers: dict[str, TierConfig] = field(default_factory=dict)
    max_review_rounds: int = 6
    per_pr_cost_ceiling_usd: float = 5.0
    daily_cost_ceiling_usd: float = 20.0
    daily_cost_warn_fraction: float = 0.8
    escalation_cooldown_minutes: int = 10
    # Per-provider model selection (empty = client default). Resolved from
    # env > repo config > "" so a cheaper model can be pinned per repo while an
    # operator env var still overrides. The spend ledger prices the chosen model.
    claude_model: str = ""
    gpt_model: str = ""
    gemini_model: str = ""
    # Billing (see usage.py): how this tenant is charged for AI usage.
    billing_mode: str = "byok"
    usage_markup_multiplier: float = 1.0
    dev_fee_usd: float = 0.0

    # The full per-repo config. main passes this to classify() so path/token
    # classification is project-specific. Defaults to generic values.
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
# Optional second location tried when the primary file is absent, so repos
# still carrying the old .github/ai-peer-review.json keep working unchanged.
REPO_CONFIG_LEGACY_PATH_ENV = "REPO_CONFIG_LEGACY_PATH"


def load_from_env(env: Optional[dict] = None) -> DispatcherConfig:
    """Build a DispatcherConfig from the repo config file and environment.

    Precedence for project metadata, rosters, and ceilings:
      explicit env var (if set)  >  repo config file  >  RepoConfig default

    Secrets always come from env (they are GitHub Actions secrets and never
    live in a committed config file). The repo config file is optional; when
    absent, generic deny-first defaults apply and project_name falls back to
    the repo name.
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

    rc = resolve_repo_config(
        e.get(REPO_CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH),
        e.get(REPO_CONFIG_LEGACY_PATH_ENV),
    )

    repo_name = _repo_name_from_env(e)

    # env override > config file value > generic fallback
    # project_name falls back to the repo name so escalation pings/labels are
    # never blank, even for a tenant that ships no config.
    project_name = e.get("PROJECT_NAME") or rc.project_name or repo_name
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
    daily_warn_fraction = (
        float(e["DAILY_COST_WARN_FRACTION"])
        if e.get("DAILY_COST_WARN_FRACTION")
        else rc.daily_cost_warn_fraction
    )
    cooldown = (
        int(e["ESCALATION_COOLDOWN_MINUTES"])
        if e.get("ESCALATION_COOLDOWN_MINUTES")
        else rc.escalation_cooldown_minutes
    )
    billing_mode = (e.get("BILLING_MODE") or rc.billing_mode or "byok").strip().lower()
    usage_markup = (
        float(e["USAGE_MARKUP_MULTIPLIER"])
        if e.get("USAGE_MARKUP_MULTIPLIER")
        else rc.usage_markup_multiplier
    )
    dev_fee = float(e["DEV_FEE_USD"]) if e.get("DEV_FEE_USD") else rc.dev_fee_usd
    # Model selection: env var (operator override) > repo config file > default.
    claude_model = (e.get("ANTHROPIC_MODEL") or rc.claude_model or "").strip()
    gpt_model = (e.get("OPENAI_MODEL") or rc.gpt_model or "").strip()
    gemini_model = (e.get("GEMINI_MODEL") or rc.gemini_model or "").strip()
    # Escalation email sender: env override > repo config > "" (default sender).
    email_from = (e.get("EMAIL_FROM") or rc.email_from or "").strip()

    return DispatcherConfig(
        project_name=project_name,
        operator_email=(e.get("OPERATOR_EMAIL", "") or "").strip(),
        operator_github_login=operator_login,
        repo_owner=e.get("GITHUB_REPOSITORY_OWNER", "NFS-247"),
        repo_name=repo_name,
        anthropic_api_key=secret("ANTHROPIC_API_KEY"),
        openai_api_key=secret("OPENAI_API_KEY"),
        gemini_api_key=secret("GEMINI_API_KEY"),
        resend_api_key=secret("RESEND_API_KEY"),
        github_token=secret("GITHUB_TOKEN"),
        verdict_secret=secret("DISPATCHER_VERDICT_SECRET"),
        google_chat_webhook_url=secret("GOOGLE_CHAT_WEBHOOK_URL"),
        approve_webapp_url=secret("APPROVE_WEBAPP_URL"),
        approve_signing_secret=secret("APPROVE_SIGNING_SECRET"),
        email_from=email_from,
        github_app_id=(e.get("GITHUB_APP_ID") or "").strip(),
        github_app_private_key=(secret("GITHUB_APP_PRIVATE_KEY") or ""),
        tiers=tiers_from_repo_config(rc),
        max_review_rounds=max_rounds,
        per_pr_cost_ceiling_usd=per_pr_ceiling,
        daily_cost_ceiling_usd=daily_ceiling,
        daily_cost_warn_fraction=daily_warn_fraction,
        escalation_cooldown_minutes=cooldown,
        billing_mode=billing_mode,
        usage_markup_multiplier=usage_markup,
        dev_fee_usd=dev_fee,
        claude_model=claude_model,
        gpt_model=gpt_model,
        gemini_model=gemini_model,
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
