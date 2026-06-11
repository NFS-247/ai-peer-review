"""Per-repository configuration for the AI peer review dispatcher.

This is what makes the dispatcher multi-tenant: every project-specific rule —
high-stakes path patterns, content-scan safety tokens, head-lock paths,
reviewer rosters, ceilings, project name — is loaded here from the consuming
repo's config file, overlaid onto GENERIC defaults. The defaults are
deliberately project-agnostic (deny-first); a trading system's broker rules or
a payments app's billing rules live in that repo's config, not in this code.

Format: JSON, at ``.peer-review.json`` in the consuming repo's root (legacy
location ``.github/ai-peer-review.json`` is still honored). JSON (not YAML) is
deliberate: the dispatcher is pure-stdlib with no external dependencies, and
``json`` is in the standard library while a YAML parser is not. Any field
omitted from the file falls back to the generic default. (The original Cut-1
spec named a ``.peer-review.yml``; JSON-at-root was chosen instead to preserve
the zero-dependency / stdlib-only invariant that the standalone test enforces.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from .classify import (
    BACKEND_PATH_ROOTS,
    CONTENT_SAFETY_PATTERNS,
    CONTENT_SAFETY_TOKENS,
    HIGH_STAKES_PATH_PATTERNS,
    ROUTINE_PATH_PATTERNS,
    ROUTINE_ROOT_EXACT,
    ROUTINE_ROOT_GLOBS,
)


DEFAULT_CONFIG_PATH = ".peer-review.json"
# Older location, still honored so existing tenants don't need a flag day.
LEGACY_CONFIG_PATHS: tuple[str, ...] = (".github/ai-peer-review.json",)


@dataclass(frozen=True)
class RepoConfig:
    """All project-specific dispatcher configuration for one repository.

    Defaults are GENERIC and project-agnostic. A repo with no config file is
    still safe (deny-first: unknown paths -> high_stakes), but carries none of
    any one project's specific rules. ``project_name`` defaults to empty; the
    config loader falls back to the repo name so escalations are still labeled.
    ``operator_github_login`` defaults to the NFS-247 operator (Cut 1 is
    NFS-247-only); override it per repo as needed.
    """

    project_name: str = ""
    operator_github_login: str = "NERT24"
    # Verified Resend sender for escalation emails, e.g.
    # "Project Alerts <alerts@yourdomain.com>". MUST be an address on a domain
    # verified in your Resend account — the default onboarding@resend.dev sender
    # only delivers to the Resend account's own email, so leaving this empty
    # makes every escalation email fail (the PR-comment fallback still fires).
    email_from: str = ""

    # Domain context injected into the reviewer prompt so reviews are tailored
    # per tenant instead of hard-coded to one project. project_description is a
    # sentence or two about what the repo is; review_guidance is a list of
    # project-specific bug classes for reviewers to hunt for.
    project_description: str = ""
    review_guidance: tuple[str, ...] = ()

    # Classification (mirrors classify.py defaults).
    high_stakes_paths: tuple[str, ...] = HIGH_STAKES_PATH_PATTERNS
    content_scan_safety_tokens: tuple[str, ...] = CONTENT_SAFETY_TOKENS
    content_scan_safety_patterns: tuple[str, ...] = CONTENT_SAFETY_PATTERNS
    routine_root_exact: tuple[str, ...] = ROUTINE_ROOT_EXACT
    routine_root_globs: tuple[str, ...] = ROUTINE_ROOT_GLOBS
    routine_path_patterns: tuple[str, ...] = ROUTINE_PATH_PATTERNS
    backend_path_roots: tuple[str, ...] = BACKEND_PATH_ROOTS

    # Paths requiring explicit operator sign-off before merge even when the AI
    # reviewers converge (project-specific; generic default is empty).
    head_lock_paths: tuple[str, ...] = ()

    # Reviewer rosters per tier.
    routine_reviewers: tuple[str, ...] = ("claude", "gpt")
    backend_reviewers: tuple[str, ...] = ("claude", "gpt")
    high_stakes_reviewers: tuple[str, ...] = ("claude", "gpt", "gemini")

    # Optional per-provider model override. Empty = the client default (which
    # itself honors the ANTHROPIC_MODEL / OPENAI_MODEL / GEMINI_MODEL env var,
    # else the built-in default). Lets a tenant pick a cheaper model in its own
    # committed config; the spend ledger automatically prices whatever model is
    # chosen. An env var, if set, still wins over the file (operator override).
    claude_model: str = ""
    gpt_model: str = ""
    gemini_model: str = ""

    # Budgets and ceilings.
    max_review_rounds: int = 6
    routine_round_budget: int = 3
    backend_round_budget: int = 3
    high_stakes_round_budget: int = 2
    per_pr_cost_ceiling_usd: float = 5.0
    daily_cost_ceiling_usd: float = 20.0
    # Fraction of the daily ceiling at which to send a one-time "budget almost
    # spent" warning ping, so the operator can throttle before reviews pause.
    # 0 disables the pre-warning. Default 0.8 (80%).
    daily_cost_warn_fraction: float = 0.8

    # Minutes the dev agent must be quiet (no new commit) before a review-
    # outcome escalation pings the operator. 0 disables the cooldown (ping
    # immediately — the pre-Cut-1 behavior).
    escalation_cooldown_minutes: int = 10

    # Billing (see usage.py). Defaults are no-ops: "byok" = the tenant brings
    # their own keys and pays the providers directly, so the platform charges
    # nothing for usage; markup 1.0 and dev_fee 0 add nothing. Flip to
    # "platform" + a markup to bill the tenant for the AI it consumes.
    billing_mode: str = "byok"
    usage_markup_multiplier: float = 1.0
    dev_fee_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "operator_github_login": self.operator_github_login,
            "email_from": self.email_from,
            "project_description": self.project_description,
            "review_guidance": list(self.review_guidance),
            "high_stakes_paths": list(self.high_stakes_paths),
            "content_scan_safety_tokens": list(self.content_scan_safety_tokens),
            "content_scan_safety_patterns": list(self.content_scan_safety_patterns),
            "routine_root_exact": list(self.routine_root_exact),
            "routine_root_globs": list(self.routine_root_globs),
            "routine_path_patterns": list(self.routine_path_patterns),
            "backend_path_roots": list(self.backend_path_roots),
            "head_lock_paths": list(self.head_lock_paths),
            "routine_reviewers": list(self.routine_reviewers),
            "backend_reviewers": list(self.backend_reviewers),
            "high_stakes_reviewers": list(self.high_stakes_reviewers),
            "claude_model": self.claude_model,
            "gpt_model": self.gpt_model,
            "gemini_model": self.gemini_model,
            "max_review_rounds": self.max_review_rounds,
            "routine_round_budget": self.routine_round_budget,
            "backend_round_budget": self.backend_round_budget,
            "high_stakes_round_budget": self.high_stakes_round_budget,
            "per_pr_cost_ceiling_usd": self.per_pr_cost_ceiling_usd,
            "daily_cost_ceiling_usd": self.daily_cost_ceiling_usd,
            "daily_cost_warn_fraction": self.daily_cost_warn_fraction,
            "escalation_cooldown_minutes": self.escalation_cooldown_minutes,
            "billing_mode": self.billing_mode,
            "usage_markup_multiplier": self.usage_markup_multiplier,
            "dev_fee_usd": self.dev_fee_usd,
        }


_STR_TUPLE_FIELDS = {
    "high_stakes_paths",
    "content_scan_safety_tokens",
    "content_scan_safety_patterns",
    "routine_root_exact",
    "routine_root_globs",
    "routine_path_patterns",
    "backend_path_roots",
    "head_lock_paths",
    "review_guidance",
    "routine_reviewers",
    "backend_reviewers",
    "high_stakes_reviewers",
}
_STR_FIELDS = {
    "project_name",
    "operator_github_login",
    "email_from",
    "project_description",
    "billing_mode",
    "claude_model",
    "gpt_model",
    "gemini_model",
}
_INT_FIELDS = {
    "max_review_rounds",
    "routine_round_budget",
    "backend_round_budget",
    "high_stakes_round_budget",
    "escalation_cooldown_minutes",
}
_FLOAT_FIELDS = {
    "per_pr_cost_ceiling_usd",
    "daily_cost_ceiling_usd",
    "daily_cost_warn_fraction",
    "usage_markup_multiplier",
    "dev_fee_usd",
}

_KNOWN_FIELDS = _STR_TUPLE_FIELDS | _STR_FIELDS | _INT_FIELDS | _FLOAT_FIELDS


def from_mapping(data: dict[str, Any]) -> RepoConfig:
    """Build a RepoConfig from a parsed mapping, overlaying onto defaults.

    Unknown keys are ignored (forward/backward compatible). Wrong-typed values
    raise ValueError so a malformed config is caught loudly rather than
    silently misclassifying.
    """
    overrides: dict[str, Any] = {}
    for key, value in data.items():
        if key not in _KNOWN_FIELDS:
            continue  # ignore unknown keys
        if key in _STR_TUPLE_FIELDS:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"config field {key!r} must be a list of strings")
            overrides[key] = tuple(value)
        elif key in _STR_FIELDS:
            if not isinstance(value, str):
                raise ValueError(f"config field {key!r} must be a string")
            overrides[key] = value
        elif key in _INT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"config field {key!r} must be an int")
            overrides[key] = value
        elif key in _FLOAT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"config field {key!r} must be a number")
            overrides[key] = float(value)

    return replace(RepoConfig(), **overrides)


def load_repo_config(path: str | Path = DEFAULT_CONFIG_PATH) -> RepoConfig:
    """Load the repo config from ``path`` if present, else return defaults.

    A missing file is the normal case for a repo that wants the generic
    defaults. A present-but-malformed file raises ValueError (fail loud).
    """
    p = Path(path)
    if not p.exists():
        return RepoConfig()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"config file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must be a JSON object")
    return from_mapping(data)


def resolve_repo_config(*candidate_paths: str | Path | None) -> RepoConfig:
    """Load the first config file that exists among ``candidate_paths``.

    Lets the caller pass the primary path (``.peer-review.json`` at root)
    followed by legacy locations (``.github/ai-peer-review.json``). The first
    file that exists is loaded (and validated — a present-but-malformed file
    still fails loud). If none exist, generic defaults are returned.
    """
    for candidate in candidate_paths:
        if candidate and Path(candidate).exists():
            return load_repo_config(candidate)
    return RepoConfig()


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "LEGACY_CONFIG_PATHS",
    "RepoConfig",
    "from_mapping",
    "load_repo_config",
    "resolve_repo_config",
]
