"""Repository context for reviewers: the existing code a change must not break.

The AI reviewers otherwise see only the PR diff — so they cannot tell whether a
change breaks a CALLER that lives in an unchanged file. This module reads the
base-branch checkout (``GITHUB_WORKSPACE`` — the caller repo's default branch, the
same tree the dispatcher already uses for ``.peer-review.json``) and assembles two
things for the prompt:

  1. a REPO MAP — the source-file tree, so the reviewer knows what exists; and
  2. REFERENCES — for each symbol this PR defines/changes (functions, classes…),
     the existing code elsewhere in the repo that uses that name (the "blast
     radius"), so the reviewer can check the change doesn't break its callers.

Trust: it reads the BASE branch only, never the PR head. The canonical caller
workflow checks out ``ref: default_branch`` into ``GITHUB_WORKSPACE`` — but rather
than rely on that alone, ``build_repository_context`` is handed the PR head SHA and
REFUSES to read the tree if the checkout is the PR head (``.git/HEAD`` resolves to
that SHA). So a misconfigured caller that checked out the PR head can't inject
attacker-controlled files into the prompt. Everything is bounded by a char budget
so it can't blow up token cost; on a missing/empty/untrusted workspace it returns
"" and the feature no-ops.

Pure / stdlib-only (os, re, pathlib): takes a root path, returns a prompt string.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Sequence


# Directories never worth scanning (VCS, caches, vendored deps, build output).
_IGNORE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode", ".tox", "site-packages", "vendor", ".next",
    "target", "coverage", ".gradle",
})

# Source extensions we map + search. A deliberate allowlist so binaries, images,
# and lockfiles never enter the prompt.
_SOURCE_EXTS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".java", ".kt",
    ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".swift",
    ".scala", ".sh", ".sql", ".gs",
})

# Symbols this diff DEFINES or changes — best-effort across languages. Matches a
# def/class/func/etc. on an added or removed diff line and captures the name.
_DEF_RE = re.compile(
    r"^[+-]\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|static\s+|async\s+)*"
    r"(?:def|class|func|function|interface|type|struct|enum|trait|fn)\s+([A-Za-z_]\w{3,})"
)
# Assignment-style exports/consts: `export const Foo =`, `Foo = function`, etc.
_ASSIGN_RE = re.compile(
    r"^[+-]\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w{3,})\s*="
)

# Names that match too broadly to be useful blast-radius signal: nearly every
# class has these, so "every caller of parse/build/close" would bury the real
# callers under the result cap. Skipped (plus all dunders). Reference search on a
# specific name (its own value) is what makes the feature useful; a generic name
# can't be localized by whole-word matching alone, so it is noise, not signal.
_GENERIC_NAMES = frozenset({
    "main", "setup", "setUp", "tearDown", "parse", "build", "close", "open",
    "read", "write", "send", "load", "save", "start", "stop", "handle",
    "process", "update", "create", "delete", "remove", "render", "execute",
    "validate", "name", "value", "data", "result", "config", "client",
    "server", "request", "response", "init", "call", "make", "test", "run",
})

# Caps so one review can never walk a giant repo or balloon the prompt.
_MAX_MAP_FILES = 1500       # source paths listed in the map before a truncation marker
_WALK_CAP = 20000           # backstop on files discovered (pathological repos only)
_MAX_SYMBOLS = 40           # changed symbols we look up
_MAX_REF_FILES = 25         # files included in the references section
_MAX_REF_LINES_PER_FILE = 10
_MAX_LINE_CHARS = 240
_DEFAULT_BUDGET_CHARS = 30000


def _rel(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def _iter_source_files(root: Path, *, cap: int = _WALK_CAP) -> list[Path]:
    """Source files under ``root`` (VCS/cache/vendor pruned), alphabetical order.

    The whole tree is walked; ``cap`` is only a backstop against a pathological
    repo, not an expected limit, so callers in late-alphabet directories are not
    systematically missed.
    """
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored / hidden dirs in place so os.walk doesn't descend into them.
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORE_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if Path(name).suffix.lower() in _SOURCE_EXTS:
                out.append(Path(dirpath) / name)
                if len(out) >= cap:
                    return out
    return out


def changed_symbols(diff_text: str) -> list[str]:
    """Names this diff defines/changes (functions, classes, exported consts).

    Best-effort and language-heuristic. Skips names shorter than 4 chars, dunders
    (``__init__`` & friends — everywhere, no signal), and a stop-list of generic
    method names (``parse``/``build``/…) that whole-word matching can't localize,
    so they don't crowd real callers out of the bounded references section.
    Order-stable, de-duplicated, and capped.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for line in diff_text.splitlines():
        if not line or line[0] not in "+-":
            continue
        m = _DEF_RE.match(line) or _ASSIGN_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        if len(name) < 4 or name.startswith("__") or name in _GENERIC_NAMES:
            continue
        if name not in seen:
            seen.add(name)
            ordered.append(name)
            if len(ordered) >= _MAX_SYMBOLS:
                break
    return ordered


def find_references(
    root: Path,
    symbols: Sequence[str],
    *,
    exclude: Iterable[str] = (),
    max_ref_files: int = _MAX_REF_FILES,
    max_lines_per_file: int = _MAX_REF_LINES_PER_FILE,
) -> list[tuple[str, list[str]]]:
    """Files (excluding ``exclude``) that mention any of ``symbols``, with excerpts.

    Returns ``[(relpath, ["L12: ...", ...]), ...]``. A line matches when a symbol
    appears as a whole word. The changed files themselves are excluded so the
    result is the EXTERNAL blast radius. The whole tree is walked (discovery is
    not truncated); ``max_ref_files`` caps how many matching files are RETURNED.
    """
    if not symbols:
        return []
    word_res = [re.compile(rf"\b{re.escape(s)}\b") for s in symbols]
    excluded = {e.replace("\\", "/") for e in exclude}
    results: list[tuple[str, list[str]]] = []
    for path in _iter_source_files(root):
        rel = _rel(root, path)
        if rel in excluded:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        hits: list[str] = []
        for i, line in enumerate(text.splitlines(), start=1):
            if any(r.search(line) for r in word_res):
                hits.append(f"L{i}: {line.strip()[:_MAX_LINE_CHARS]}")
                if len(hits) >= max_lines_per_file:
                    break
        if hits:
            results.append((rel, hits))
            if len(results) >= max_ref_files:
                break
    return results


def _checkout_is_pr_head(root: Path, head_sha: str) -> bool:
    """True if the checkout at ``root`` is the PR HEAD commit (untrusted).

    The dispatcher must read base-branch code only. The canonical caller workflow
    checks out the default branch, so HEAD is never the PR head — but this is the
    enforced backstop: a checkout of a specific commit (how a PR head is pulled)
    leaves ``.git/HEAD`` holding that raw SHA, so if it equals the PR head we
    refuse to read the tree. A branch checkout leaves a symbolic ``ref: …`` (never
    equal to a SHA), and any read failure is treated as "not provably the head" so
    the documented base-checkout setup is unaffected.
    """
    if not head_sha:
        return False
    try:
        head_ref = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return head_ref == head_sha.strip()


def build_repository_context(
    *,
    root: str | os.PathLike | None,
    changed_files: Sequence[str],
    diff_text: str,
    head_sha: str = "",
    budget_chars: int = _DEFAULT_BUDGET_CHARS,
) -> str:
    """Assemble the prompt 'existing code on the base branch' section, or "".

    ``root`` is the base-branch checkout (typically ``$GITHUB_WORKSPACE``).
    Returns "" when ``root`` is unset/missing/empty, when ``budget_chars`` <= 0,
    or when the checkout is provably the PR head (``head_sha`` — untrusted, so it
    is refused). Otherwise it always emits the repo map (orientation); the
    callers/blast-radius block is added only when the diff defines recognizable
    symbols. The whole section is capped at ``budget_chars`` with an explicit
    truncation marker, and the map shows an honest "N of M files" marker when the
    repo has more source files than fit.
    """
    if not root or budget_chars <= 0:
        return ""
    root_path = Path(root)
    if not root_path.is_dir():
        return ""
    if _checkout_is_pr_head(root_path, head_sha):
        return ""

    all_files = _iter_source_files(root_path)
    if not all_files:
        return ""
    total = len(all_files)
    shown = all_files[:_MAX_MAP_FILES]
    file_map = "\n".join(_rel(root_path, p) for p in shown)
    if total > len(shown):
        file_map += f"\n… (showing {len(shown)} of {total} source files)"

    symbols = changed_symbols(diff_text)
    refs = find_references(root_path, symbols, exclude=changed_files)

    parts: list[str] = [
        "Repository context — the existing code on the base branch (the current "
        "main, NOT this PR's changes). Use it to check the change is consistent "
        "with how the codebase already works and does not break existing callers.",
        "",
        "Repo file map:",
        file_map,
    ]

    if refs:
        sym_list = ", ".join(symbols)
        parts.append("")
        parts.append(
            f"Existing code that references what this PR changes "
            f"({sym_list}) — verify the change doesn't break these callers:"
        )
        for rel, hits in refs:
            parts.append("")
            parts.append(f"=== {rel} ===")
            parts.extend(hits)

    section = "\n".join(parts)
    if len(section) > budget_chars:
        section = (
            section[:budget_chars].rstrip()
            + "\n… (repository context truncated to fit the review budget)"
        )
    return section


__all__ = [
    "build_repository_context",
    "changed_symbols",
    "find_references",
]
