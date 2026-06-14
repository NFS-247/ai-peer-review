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

Trust: it reads ONLY the base branch (main), never the PR head, so a PR author
can't plant a huge or malicious file into the prompt — the context is trusted
code that already shipped. Everything is bounded by a char budget so it can't blow
up token cost; on a missing/empty workspace it returns "" and the feature no-ops.

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

# Caps so one review can never walk a giant repo or balloon the prompt.
_MAX_MAP_FILES = 400        # paths listed in the repo map
_MAX_SCAN_FILES = 4000      # files opened when searching for references
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


def _iter_source_files(root: Path, *, max_files: int) -> list[Path]:
    """Source files under ``root``, ignoring VCS/cache/vendor dirs. Bounded."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored dirs in place so os.walk doesn't descend into them.
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORE_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if Path(name).suffix.lower() in _SOURCE_EXTS:
                out.append(Path(dirpath) / name)
                if len(out) >= max_files:
                    return out
    return out


def changed_symbols(diff_text: str) -> list[str]:
    """Names this diff defines/changes (functions, classes, exported consts).

    Best-effort and language-heuristic. Names shorter than 4 chars are skipped
    (they match too broadly to be useful blast-radius signal). Order-stable and
    de-duplicated; capped so a huge diff can't explode the reference search.
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
        if len(name) >= 4 and name not in seen:
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
    max_scan_files: int = _MAX_SCAN_FILES,
    max_ref_files: int = _MAX_REF_FILES,
    max_lines_per_file: int = _MAX_REF_LINES_PER_FILE,
) -> list[tuple[str, list[str]]]:
    """Files (excluding ``exclude``) that mention any of ``symbols``, with excerpts.

    Returns ``[(relpath, ["L12: ...", ...]), ...]``. A line matches when a symbol
    appears as a whole word. Self-references (the changed files themselves) are
    excluded so the result is the EXTERNAL blast radius. Bounded on every axis.
    """
    if not symbols:
        return []
    word_res = {s: re.compile(rf"\b{re.escape(s)}\b") for s in symbols}
    excluded = {e.replace("\\", "/") for e in exclude}
    results: list[tuple[str, list[str]]] = []
    scanned = 0
    for path in _iter_source_files(root, max_files=max_scan_files):
        rel = _rel(root, path)
        if rel in excluded:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        hits: list[str] = []
        for i, line in enumerate(text.splitlines(), start=1):
            if any(r.search(line) for r in word_res.values()):
                stripped = line.strip()[:_MAX_LINE_CHARS]
                hits.append(f"L{i}: {stripped}")
                if len(hits) >= max_lines_per_file:
                    break
        if hits:
            results.append((rel, hits))
            if len(results) >= max_ref_files:
                break
    return results


def build_repository_context(
    *,
    root: str | os.PathLike | None,
    changed_files: Sequence[str],
    diff_text: str,
    budget_chars: int = _DEFAULT_BUDGET_CHARS,
) -> str:
    """Assemble the prompt 'existing code on the base branch' section, or "".

    ``root`` is the base-branch checkout (typically ``$GITHUB_WORKSPACE``). Returns
    "" when it is unset/missing (the feature simply no-ops), or when the diff
    defines no recognizable symbols and the map would add nothing useful. The
    whole section is capped at ``budget_chars``.
    """
    if not root:
        return ""
    root_path = Path(root)
    if not root_path.is_dir():
        return ""

    files = _iter_source_files(root_path, max_files=_MAX_MAP_FILES)
    if not files:
        return ""
    file_map = "\n".join(_rel(root_path, p) for p in files)

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
