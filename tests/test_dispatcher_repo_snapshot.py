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
    assert len(ctx) <= 300              # final string (incl. marker) stays within budget
    assert "truncated" in ctx


def test_changed_symbols_skips_dunders_and_generic_names():
    diff = "+def __init__(self):\n+def parse(self):\n+class build:\n+def calculate_total(x):\n"
    syms = RS.changed_symbols(diff)
    assert "__init__" not in syms        # dunder: everywhere, no signal
    assert "parse" not in syms           # generic stop-list
    assert "build" not in syms           # generic stop-list
    assert "calculate_total" in syms     # a specific name is still surfaced


def test_zero_budget_returns_empty(tmp_path):
    _mk(tmp_path, "a.py", "def thing():\n    pass\n")
    assert RS.build_repository_context(
        root=str(tmp_path), changed_files=[], diff_text="+def thing():\n", budget_chars=0
    ) == ""


def _set_head(tmp_path, value):
    p = tmp_path / ".git" / "HEAD"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value + "\n", encoding="utf-8")


def test_refuses_untrusted_checkouts(tmp_path):
    # Allowlist trust: only a provable base checkout is read. A PR-branch ref, a
    # merge ref, a detached PR-head commit, and an unreadable HEAD are all refused.
    _mk(tmp_path, "a.py", "def calculate_total(x):\n    return x\n")
    diff = "+def calculate_total(x):\n"
    kw = dict(changed_files=[], diff_text=diff, trusted_refs=["main"], base_sha="basesha0")

    _set_head(tmp_path, "ref: refs/heads/feature-x")          # PR head branch
    assert RS.build_repository_context(root=str(tmp_path), **kw) == ""
    _set_head(tmp_path, "ref: refs/pull/3/merge")             # PR merge ref
    assert RS.build_repository_context(root=str(tmp_path), **kw) == ""
    _set_head(tmp_path, "prheadsha9")                         # detached at PR head
    assert RS.build_repository_context(root=str(tmp_path), **kw) == ""
    # No .git/HEAD at all -> can't prove base -> refused.
    (tmp_path / ".git" / "HEAD").unlink()
    assert RS.build_repository_context(root=str(tmp_path), **kw) == ""


def test_accepts_trusted_base_checkouts(tmp_path):
    _mk(tmp_path, "a.py", "def calculate_total(x):\n    return x\n")
    diff = "+def calculate_total(x):\n"

    # A branch checkout of a trusted ref (default branch / PR base) is read.
    _set_head(tmp_path, "ref: refs/heads/main")
    assert "Repo file map:" in RS.build_repository_context(
        root=str(tmp_path), changed_files=[], diff_text=diff,
        trusted_refs=["main", "release"], base_sha="basesha0",
    )
    # A detached checkout at the base SHA is read.
    _set_head(tmp_path, "basesha0")
    assert "Repo file map:" in RS.build_repository_context(
        root=str(tmp_path), changed_files=[], diff_text=diff,
        trusted_refs=["main"], base_sha="basesha0",
    )


def test_no_trust_params_skips_check_for_vouching_caller(tmp_path):
    # With no trust identifiers the builder skips the checkout guard (the caller
    # vouches for the root); the orchestrator always passes them in production.
    _mk(tmp_path, "a.py", "def calculate_total(x):\n    return x\n")
    ctx = RS.build_repository_context(
        root=str(tmp_path), changed_files=[], diff_text="+def calculate_total(x):\n",
    )
    assert "Repo file map:" in ctx


def test_map_shows_honest_truncation_marker(tmp_path, monkeypatch):
    for i in range(6):
        _mk(tmp_path, f"f{i}.py", "x = 1\n")
    monkeypatch.setattr(RS, "_MAX_MAP_FILES", 2)
    ctx = RS.build_repository_context(root=str(tmp_path), changed_files=[], diff_text="")
    assert "showing 2 of 6 source files" in ctx


def test_caller_found_regardless_of_alphabetical_position(tmp_path):
    # Discovery walks the WHOLE tree: a caller in a late-named dir is still found
    # behind many earlier non-referencing files (the cap is on results, not walk).
    for i in range(30):
        _mk(tmp_path, f"aaa_{i:02d}.py", "x = 1\n")
    _mk(tmp_path, "core.py", "def apply_cadence(n):\n    return n\n")
    _mk(tmp_path, "zzz_last/caller.py", "from core import apply_cadence\napply_cadence(1)\n")
    refs = RS.find_references(tmp_path, ["apply_cadence"], exclude=["core.py"])
    assert "zzz_last/caller.py" in [p for p, _ in refs]
