"""Tests for scripts.dispatcher.repo_snapshot (repository context for reviewers).

The reviewers otherwise see only the diff; this builds a bounded view of the
existing base-branch code a change must not break (a repo map + the callers of
what the PR changes), read from the trusted base checkout — never the PR head.
"""

from pathlib import Path

from scripts.dispatcher import repo_snapshot as RS


def _mk(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ---- changed_symbols --------------------------------------------------------

def test_changed_symbols_extracts_defs_classes_and_exports():
    diff = (
        "--- a/x.py\n+++ b/x.py\n"
        "+def calculate_total(items):\n"
        "+class OrderBook:\n"
        "-def old_helper():\n"
        "+    x = 1\n"  # not a definition
        "+export const ApplyCadence = () => {}\n"
    )
    syms = RS.changed_symbols(diff)
    assert "calculate_total" in syms
    assert "OrderBook" in syms
    assert "old_helper" in syms       # removed defs count too (their callers may break)
    assert "ApplyCadence" in syms
    assert "x" not in syms            # plain assignment is not a symbol def


def test_changed_symbols_skips_short_names_and_dedupes():
    diff = "+def ab(x):\n+def calc(y):\n+def calc(z):\n"
    syms = RS.changed_symbols(diff)
    assert "ab" not in syms           # < 4 chars: too broad to be useful
    assert syms.count("calc") == 1    # de-duplicated


# ---- find_references --------------------------------------------------------

def test_find_references_finds_callers_and_excludes_changed(tmp_path):
    _mk(tmp_path, "app/core.py", "def calculate_total(x):\n    return x\n")
    _mk(tmp_path, "app/caller.py", "from app.core import calculate_total\nv = calculate_total(3)\n")
    _mk(tmp_path, "app/unrelated.py", "def other():\n    return 1\n")
    refs = RS.find_references(tmp_path, ["calculate_total"], exclude=["app/core.py"])
    paths = [p for p, _ in refs]
    assert "app/caller.py" in paths
    assert "app/core.py" not in paths        # the changed file itself is excluded
    assert "app/unrelated.py" not in paths    # no reference -> not included


def test_find_references_empty_without_symbols(tmp_path):
    _mk(tmp_path, "a.py", "x = 1\n")
    assert RS.find_references(tmp_path, []) == []


# ---- build_repository_context ----------------------------------------------

def test_context_empty_for_missing_root():
    assert RS.build_repository_context(root=None, changed_files=[], diff_text="") == ""
    assert RS.build_repository_context(
        root="/no/such/dir/zzz", changed_files=[], diff_text=""
    ) == ""


def test_context_includes_map_and_caller_blast_radius(tmp_path):
    _mk(tmp_path, "app/core.py", "def apply_cadence(n):\n    return n\n")
    _mk(tmp_path, "app/ui.py", "from app.core import apply_cadence\napply_cadence(5)\n")
    diff = (
        "--- a/app/core.py\n+++ b/app/core.py\n"
        "+def apply_cadence(n):\n+    return n + 1\n"
    )
    ctx = RS.build_repository_context(
        root=str(tmp_path), changed_files=["app/core.py"], diff_text=diff
    )
    assert "Repo file map:" in ctx
    assert "app/core.py" in ctx and "app/ui.py" in ctx      # the map
    assert "references what this PR changes" in ctx
    assert "apply_cadence" in ctx
    assert "=== app/ui.py ===" in ctx                       # the caller, surfaced
    assert "base branch" in ctx                             # framing: not the PR head


def test_context_ignores_vcs_and_vendor_dirs(tmp_path):
    _mk(tmp_path, "real.py", "def thing():\n    pass\n")
    _mk(tmp_path, ".git/config", "junk\n")
    _mk(tmp_path, "node_modules/pkg/index.js", "lots of vendored code\n")
    ctx = RS.build_repository_context(root=str(tmp_path), changed_files=[], diff_text="")
    assert "real.py" in ctx
    assert ".git" not in ctx
    assert "node_modules" not in ctx


def test_context_truncates_to_budget(tmp_path):
    _mk(tmp_path, "core.py", "def apply_cadence(n):\n    return n\n")
    for i in range(40):
        _mk(tmp_path, f"caller_{i}.py", "from core import apply_cadence\napply_cadence(1)\n")
    diff = "+def apply_cadence(n):\n"
    ctx = RS.build_repository_context(
        root=str(tmp_path), changed_files=["core.py"], diff_text=diff, budget_chars=300
    )
    assert len(ctx) <= 300 + 100        # capped near budget (+ the truncation note)
    assert "truncated" in ctx
