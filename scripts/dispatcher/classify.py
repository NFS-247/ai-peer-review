"""Tier classification for the AI peer review dispatcher.

Implements Section 4 of the design doc. The classifier reads a pull
request's changed files and diff content, returns one of three tiers:

- routine: docs only
- backend: backend code, but not safety-relevant
- high_stakes: anything that could affect safety/security, infra, the build,
  dependencies, or unknown paths

The module-level pattern/token constants are GENERIC, project-agnostic
defaults. A consuming repo tailors them via its .peer-review.json (loaded into
a RepoConfig and passed to classify()); project-specific danger rules live
there, not here. With no config, the generic defaults plus the deny-first
unknown-path rule keep classification safe.

This module is pure: it takes data in, returns a tier. No I/O, no state.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


TIER_ROUTINE = "routine"
TIER_BACKEND = "backend"
TIER_HIGH_STAKES = "high_stakes"


# ---- Path patterns ---------------------------------------------------------

# Root-level exact files and root-level-only globs. A bare "*.md" must NOT
# match markdown nested in other directories (e.g. backend/app/some_doc.md).
# Root-only patterns are checked with a no-slash guard in _match_routine.
ROUTINE_ROOT_EXACT: tuple[str, ...] = (
    "README.md",
    ".gitignore",
    "LICENSE",
)

# Root-level markdown only (no slash in the path).
ROUTINE_ROOT_GLOBS: tuple[str, ...] = (
    "*.md",
)

# Patterns that explicitly include a directory prefix; matched normally.
ROUTINE_PATH_PATTERNS: tuple[str, ...] = (
    "docs/**/*.md",
    ".github/ISSUE_TEMPLATE/**",
    ".github/PULL_REQUEST_TEMPLATE.md",
)

# GENERIC, project-agnostic defaults. These are the danger paths that are
# risky to change in almost ANY repo (CI, containerization, dependency
# manifests, infra/deploy). Project-specific danger paths — a trading system's
# broker/promotion modules, a payments app's billing code — belong in that
# repo's .peer-review.json `high_stakes_paths`, NOT here. A repo with no config
# is still safe: unknown paths default to high_stakes (deny-first) below.
HIGH_STAKES_PATH_PATTERNS: tuple[str, ...] = (
    ".github/workflows/**",
    "Dockerfile",
    "docker-compose*.yml",
    ".env",
    ".env.*",
    "deploy/**",
    "infra/**",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements*.txt",
    "requirements/*.txt",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile",
    "Gemfile.lock",
)

BACKEND_PATH_ROOTS: tuple[str, ...] = (
    "src/",
    "tests/",
)


# ---- Content tokens --------------------------------------------------------

# Generic, universally-dangerous content signals. Project-specific safety
# tokens (e.g. a trading system's `paper_only` / `live_orders_enabled`) go in
# the repo's .peer-review.json `content_scan_safety_tokens`.
CONTENT_SAFETY_TOKENS: tuple[str, ...] = (
    "force_push",
    "--no-verify",
    "DROP TABLE",
    "private_key",
    "secret_key",
    "BEGIN RSA PRIVATE KEY",
    "BEGIN OPENSSH PRIVATE KEY",
    "AWS_SECRET_ACCESS_KEY",
)

CONTENT_SAFETY_PATTERNS: tuple[str, ...] = (
    r"git\s+push\s+--force",
)


@dataclass
class ClassificationResult:
    """Outcome of classifying a PR."""

    tier: str
    reason: str
    matched_paths: tuple[str, ...] = field(default_factory=tuple)
    matched_tokens: tuple[str, ...] = field(default_factory=tuple)
    matched_patterns: tuple[str, ...] = field(default_factory=tuple)
    unknown_paths: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "reason": self.reason,
            "matched_paths": list(self.matched_paths),
            "matched_tokens": list(self.matched_tokens),
            "matched_patterns": list(self.matched_patterns),
            "unknown_paths": list(self.unknown_paths),
        }


def _match_any(path: str, patterns: Sequence[str]) -> bool:
    """True if the path matches any of the fnmatch-style patterns.

    Supports ``**`` as a wildcard for any depth, which fnmatch handles by
    treating ``**`` as ``*`` (matches anything including slashes when used as
    a directory wildcard).
    """
    for pat in patterns:
        if fnmatch.fnmatch(path, pat):
            return True
        # Handle "dir/**" recursive case explicitly because fnmatch's `**` is
        # not quite POSIX-glob.
        if pat.endswith("/**") and path.startswith(pat[:-3] + "/"):
            return True
    return False


def _match_routine(
    path: str,
    *,
    root_exact: Sequence[str],
    root_globs: Sequence[str],
    path_patterns: Sequence[str],
) -> bool:
    """True if the path is a routine (docs/config) file.

    Root-only patterns (``*.md`` and the exact root files) are guarded so they
    only match paths with no directory component. ``backend/app/some_doc.md``
    is therefore NOT routine — it falls through to the backend root check.
    """
    has_slash = "/" in path

    if not has_slash:
        if path in root_exact:
            return True
        for pat in root_globs:
            if fnmatch.fnmatch(path, pat):
                return True

    # Any markdown anywhere under docs/ (direct child or nested) is routine.
    if path.startswith("docs/") and path.endswith(".md"):
        return True

    # Other directory-prefixed routine patterns (.github/ templates).
    return _match_any(path, path_patterns)


def _attr(config: Any, name: str, default: Sequence[str]) -> Sequence[str]:
    """Read a sequence attribute off an optional config, else use the default.

    ``config`` is duck-typed (a RepoConfig, but classify deliberately does not
    import RepoConfig to avoid a circular import). When config is None, the
    generic module-level defaults are used.
    """
    if config is None:
        return default
    return getattr(config, name, default)


def classify(
    changed_files: Iterable[str],
    diff_text: str,
    config: Any = None,
) -> ClassificationResult:
    """Classify a PR into routine / backend / high_stakes.

    ``config`` is an optional repo_config.RepoConfig (duck-typed). When None,
    the generic module-level defaults are used; a consuming repo supplies its
    project-specific rules through its .peer-review.json.

    Order of evaluation:
    1. Content scan first. If the diff contains any safety token or matches
       any safety pattern, return high_stakes immediately.
    2. Path scan. If any file matches a high-stakes path pattern, high_stakes.
    3. Otherwise, check each file: if all match routine, routine; if any is
       under backend/ but not high-stakes, backend; if any file matches no
       pattern at all, high_stakes (unknown-path default).
    """
    high_stakes_paths = _attr(config, "high_stakes_paths", HIGH_STAKES_PATH_PATTERNS)
    content_tokens = _attr(config, "content_scan_safety_tokens", CONTENT_SAFETY_TOKENS)
    content_patterns = _attr(config, "content_scan_safety_patterns", CONTENT_SAFETY_PATTERNS)
    routine_root_exact = _attr(config, "routine_root_exact", ROUTINE_ROOT_EXACT)
    routine_root_globs = _attr(config, "routine_root_globs", ROUTINE_ROOT_GLOBS)
    routine_path_patterns = _attr(config, "routine_path_patterns", ROUTINE_PATH_PATTERNS)
    backend_roots = _attr(config, "backend_path_roots", BACKEND_PATH_ROOTS)

    files = list(changed_files)
    if not files:
        return ClassificationResult(
            tier=TIER_HIGH_STAKES,
            reason="empty_file_list",
        )

    # 1. Content scan
    matched_tokens = tuple(
        token for token in content_tokens if token in diff_text
    )
    matched_patterns = tuple(
        pat for pat in content_patterns if re.search(pat, diff_text)
    )
    if matched_tokens or matched_patterns:
        return ClassificationResult(
            tier=TIER_HIGH_STAKES,
            reason="content_scan_safety_match",
            matched_tokens=matched_tokens,
            matched_patterns=matched_patterns,
        )

    # 2. Path scan for high-stakes
    high_stakes_hits = tuple(
        path for path in files if _match_any(path, high_stakes_paths)
    )
    if high_stakes_hits:
        return ClassificationResult(
            tier=TIER_HIGH_STAKES,
            reason="path_match_high_stakes",
            matched_paths=high_stakes_hits,
        )

    # 3. Check each file for routine / backend / unknown.
    # Backend roots are checked before routine for paths that live under a
    # backend root, so a markdown file inside backend/app/ is classified
    # backend (not routine).
    routine_hits = []
    backend_hits = []
    unknown_hits = []
    for path in files:
        under_backend_root = any(path.startswith(root) for root in backend_roots)
        if under_backend_root:
            backend_hits.append(path)
        elif _match_routine(
            path,
            root_exact=routine_root_exact,
            root_globs=routine_root_globs,
            path_patterns=routine_path_patterns,
        ):
            routine_hits.append(path)
        else:
            unknown_hits.append(path)

    # Unknown paths default to high-stakes (deny-first)
    if unknown_hits:
        return ClassificationResult(
            tier=TIER_HIGH_STAKES,
            reason="unknown_path_default",
            unknown_paths=tuple(unknown_hits),
            matched_paths=tuple(routine_hits + backend_hits),
        )

    if backend_hits:
        return ClassificationResult(
            tier=TIER_BACKEND,
            reason="backend_path_match",
            matched_paths=tuple(backend_hits + routine_hits),
        )

    # Pure routine
    return ClassificationResult(
        tier=TIER_ROUTINE,
        reason="routine_path_match",
        matched_paths=tuple(routine_hits),
    )


__all__ = [
    "TIER_ROUTINE",
    "TIER_BACKEND",
    "TIER_HIGH_STAKES",
    "ClassificationResult",
    "classify",
    "ROUTINE_ROOT_EXACT",
    "ROUTINE_ROOT_GLOBS",
    "ROUTINE_PATH_PATTERNS",
    "HIGH_STAKES_PATH_PATTERNS",
    "BACKEND_PATH_ROOTS",
    "CONTENT_SAFETY_TOKENS",
    "CONTENT_SAFETY_PATTERNS",
]
