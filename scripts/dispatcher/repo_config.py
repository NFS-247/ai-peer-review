"""Per-repository configuration for the AI peer review dispatcher.

Phase 3 (extraction): this makes the dispatcher project-agnostic. Everything
that was hardcoded to TradeWatcher — high-stakes path patterns, content-scan
safety tokens, reviewer rosters, ceilings, project name — is loaded here, with
defaults that EXACTLY equal the current TradeWatcher values.

Format: JSON, at ``.github/ai-peer-review.json`` in the consuming repo. JSON
(not YAML) is deliberate: the dispatcher is pure-stdlib with no external
dependencies, and ``json`` is in the standard library while a YAML parser is
not. Any field omitted from the file falls back to the default.

A1 scope: this loader exists and is tested, but is not yet consumed by
classify/config. Wiring happens in A2/A3. Until then, StockTrader behavior is
provably unchanged.
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


DEFAULT_CONFIG_PATH = ".github/ai-peer-review.json"


@dataclass(frozen=True)
class RepoConfig:
    """All project-specific dispatcher configuration for one repository.

    Defaults equal the current TradeWatcher hardcoded values, so a repo with
    no config file behaves exactly as StockTrader does today.
    """

    project_name: str = "TradeWatcher"
    operator_github_login: str = "NERT24"

    # Classification (mirrors classify.py defaults).
    high_stakes_paths: tuple[str, ...] = HIGH_STAKES_PATH_PATTERNS
    content_scan_safety_tokens: tuple[str, ...] = CONTENT_SAFETY_TOKENS
    content_scan_safety_patterns: tuple[str, ...] = CONTENT_SAFETY_PATTERNS
    routine_root_exact: tuple[str, ...] = ROUTINE_ROOT_EXACT
    routine_root_globs: tuple[str, ...] = ROUTINE_ROOT_GLOBS
    routine_path_patterns: tuple[str, ...] = ROUTINE_PATH_PATTERNS
    backend_path_roots: tuple[str, ...] = BACKEND_PATH_ROOTS

    # Reviewer rosters per tier.
    routine_reviewers: tuple[str, ...] = ("claude", "gpt")
    backend_reviewers: tuple[str, ...] = ("claude", "gpt")
    high_stakes_reviewers: tuple[str, ...] = ("claude", "gpt", "gemini")

    # Budgets and ceilings.
    max_review_rounds: int = 6
    routine_round_budget: int = 3
    backend_round_budget: int = 3
    high_stakes_round_budget: int = 2
    per_pr_cost_ceiling_usd: float = 5.0
    daily_cost_ceiling_usd: float = 20.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "operator_github_login": self.operator_github_login,
            "high_stakes_paths": list(self.high_stakes_paths),
            "content_scan_safety_tokens": list(self.content_scan_safety_tokens),
            "content_scan_safety_patterns": list(self.content_scan_safety_patterns),
            "routine_root_exact": list(self.routine_root_exact),
            "routine_root_globs": list(self.routine_root_globs),
            "routine_path_patterns": list(self.routine_path_patterns),
            "backend_path_roots": list(self.backend_path_roots),
            "routine_reviewers": list(self.routine_reviewers),
            "backend_reviewers": list(self.backend_reviewers),
            "high_stakes_reviewers": list(self.high_stakes_reviewers),
            "max_review_rounds": self.max_review_rounds,
            "routine_round_budget": self.routine_round_budget,
            "backend_round_budget": self.backend_round_budget,
            "high_stakes_round_budget": self.high_stakes_round_budget,
            "per_pr_cost_ceiling_usd": self.per_pr_cost_ceiling_usd,
            "daily_cost_ceiling_usd": self.daily_cost_ceiling_usd,
        }


_STR_TUPLE_FIELDS = {
    "high_stakes_paths",
    "content_scan_safety_tokens",
    "content_scan_safety_patterns",
    "routine_root_exact",
    "routine_root_globs",
    "routine_path_patterns",
    "backend_path_roots",
    "routine_reviewers",
    "backend_reviewers",
    "high_stakes_reviewers",
}
_STR_FIELDS = {"project_name", "operator_github_login"}
_INT_FIELDS = {
    "max_review_rounds",
    "routine_round_budget",
    "backend_round_budget",
    "high_stakes_round_budget",
}
_FLOAT_FIELDS = {"per_pr_cost_ceiling_usd", "daily_cost_ceiling_usd"}

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

    A missing file is the normal case for a repo that wants TradeWatcher-style
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


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "RepoConfig",
    "from_mapping",
    "load_repo_config",
]
