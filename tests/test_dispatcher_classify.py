"""Tests for scripts.dispatcher.classify."""

import pathlib

from scripts.dispatcher.classify import (
    TIER_BACKEND,
    TIER_HIGH_STAKES,
    TIER_ROUTINE,
    classify,
)
from scripts.dispatcher.repo_config import load_repo_config

# StockTrader's ruleset now lives in a committed config file, not in the
# dispatcher's hard-coded defaults. Loading it here proves the port reproduces
# the old behavior (broker/promotion/paper_only -> high_stakes).
_STOCKTRADER_CFG_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "templates"
    / "peer-review.stocktrader.json"
)


def _stocktrader_cfg():
    return load_repo_config(_STOCKTRADER_CFG_PATH)


def test_pure_readme_is_routine():
    result = classify(["README.md"], diff_text="just docs")
    assert result.tier == TIER_ROUTINE


def test_docs_only_is_routine():
    result = classify(
        ["docs/foo.md", "docs/bar/baz.md", ".gitignore"],
        diff_text="nothing safety-relevant",
    )
    assert result.tier == TIER_ROUTINE


def test_backend_module_is_backend():
    # src/ is a generic backend root.
    result = classify(
        ["src/some_module.py"],
        diff_text="def foo():\n    return 1\n",
    )
    assert result.tier == TIER_BACKEND


def test_test_file_is_backend():
    result = classify(
        ["tests/test_some_module.py"],
        diff_text="def test_x():\n    assert True\n",
    )
    assert result.tier == TIER_BACKEND


def test_stocktrader_promotion_gate_is_high_stakes():
    result = classify(
        ["backend/app/promotion_gate.py"],
        diff_text="def evaluate(): pass\n",
        config=_stocktrader_cfg(),
    )
    assert result.tier == TIER_HIGH_STAKES
    assert "backend/app/promotion_gate.py" in result.matched_paths


def test_stocktrader_broker_glob_is_high_stakes():
    result = classify(
        ["backend/app/broker_adapter.py"],
        diff_text="x = 1\n",
        config=_stocktrader_cfg(),
    )
    assert result.tier == TIER_HIGH_STAKES


def test_stocktrader_research_director_glob_is_high_stakes():
    result = classify(
        ["backend/app/research_director_oos.py"],
        diff_text="x = 1\n",
        config=_stocktrader_cfg(),
    )
    assert result.tier == TIER_HIGH_STAKES


def test_stocktrader_candidate_glob_is_high_stakes():
    result = classify(
        ["backend/app/premarket_move_gte_1_candidate.py"],
        diff_text="x = 1\n",
        config=_stocktrader_cfg(),
    )
    assert result.tier == TIER_HIGH_STAKES


def test_stocktrader_backend_module_is_backend():
    # backend/app/ is a backend root in StockTrader's own config.
    result = classify(
        ["backend/app/some_module.py"],
        diff_text="def foo():\n    return 1\n",
        config=_stocktrader_cfg(),
    )
    assert result.tier == TIER_BACKEND


def test_workflows_are_high_stakes():
    result = classify(
        [".github/workflows/ai-peer-review.yml"],
        diff_text="name: foo\n",
    )
    assert result.tier == TIER_HIGH_STAKES


def test_dispatcher_self_is_high_stakes():
    result = classify(
        ["scripts/dispatcher/classify.py"],
        diff_text="x = 1\n",
    )
    assert result.tier == TIER_HIGH_STAKES


def test_pyproject_is_high_stakes():
    result = classify(
        ["pyproject.toml"],
        diff_text="[project]\nname = \"x\"\n",
    )
    assert result.tier == TIER_HIGH_STAKES


def test_requirements_is_high_stakes():
    result = classify(
        ["requirements.txt"],
        diff_text="requests==1.0\n",
    )
    assert result.tier == TIER_HIGH_STAKES


def test_stocktrader_content_scan_paper_only_is_high_stakes():
    # Even though README.md is routine by path, StockTrader's config lists
    # paper_only as a safety token, forcing high_stakes.
    result = classify(
        ["README.md"],
        diff_text="+ paper_only = True\n",
        config=_stocktrader_cfg(),
    )
    assert result.tier == TIER_HIGH_STAKES
    assert "paper_only" in result.matched_tokens


def test_stocktrader_content_scan_live_orders_enabled_is_high_stakes():
    result = classify(
        ["README.md"],
        diff_text="+ live_orders_enabled = True\n",
        config=_stocktrader_cfg(),
    )
    assert result.tier == TIER_HIGH_STAKES
    assert "live_orders_enabled" in result.matched_tokens


def test_stocktrader_content_scan_broker_method_call_pattern_is_high_stakes():
    result = classify(
        ["docs/some.md"],
        diff_text="example: broker.place_order(symbol='AAPL')\n",
        config=_stocktrader_cfg(),
    )
    assert result.tier == TIER_HIGH_STAKES


def test_content_scan_force_push_is_high_stakes():
    result = classify(
        ["docs/some.md"],
        diff_text="git push --force origin main\n",
    )
    assert result.tier == TIER_HIGH_STAKES


def test_unknown_path_defaults_to_high_stakes():
    # An unknown new top-level dir that nothing else covers.
    result = classify(
        ["someweirdtopleveldir/new_module.txt"],
        diff_text="nothing here\n",
    )
    assert result.tier == TIER_HIGH_STAKES
    assert "someweirdtopleveldir/new_module.txt" in result.unknown_paths


def test_mixed_routine_plus_high_stakes_is_high_stakes():
    result = classify(
        ["README.md", "backend/app/promotion_gate.py"],
        diff_text="x = 1\n",
    )
    assert result.tier == TIER_HIGH_STAKES


def test_unknown_backend_path_is_high_stakes_by_default():
    # backend/app/ is not a generic backend root, so an unrecognized file there
    # falls through to the deny-first unknown-path default.
    result = classify(
        ["backend/app/trading_mode.py"],
        diff_text="MODE = 'paper'\n",
    )
    assert result.tier == TIER_HIGH_STAKES


def test_empty_file_list_is_high_stakes():
    result = classify([], diff_text="")
    assert result.tier == TIER_HIGH_STAKES


def test_root_markdown_is_routine():
    result = classify(["CONTRIBUTING.md"], diff_text="docs only\n")
    assert result.tier == TIER_ROUTINE


def test_backend_markdown_is_backend_not_routine():
    # Regression for GPT review #7: *.md must be root-only. A markdown file
    # nested under a backend root (src/) must NOT be classified routine.
    result = classify(["src/some_doc.md"], diff_text="notes\n")
    assert result.tier == TIER_BACKEND


def test_nested_non_backend_markdown_is_unknown_high_stakes():
    # A markdown file nested somewhere that is neither root, docs/, nor a
    # backend root falls through to unknown-path-default = high-stakes.
    result = classify(["weird/place/notes.md"], diff_text="notes\n")
    assert result.tier == TIER_HIGH_STAKES


def test_docs_nested_markdown_is_routine():
    result = classify(["docs/sub/dir/guide.md"], diff_text="guide\n")
    assert result.tier == TIER_ROUTINE


# ---- Phase 3 A2: classification driven by a custom RepoConfig --------------

from scripts.dispatcher.repo_config import RepoConfig


def test_custom_config_changes_high_stakes_paths():
    # A vendor-app config: billing/auth are high-stakes; TradeWatcher's
    # backend/app/promotion_gate.py is NOT special here.
    cfg = RepoConfig(
        project_name="Vendor Intelligence",
        high_stakes_paths=("src/billing/**", "src/auth/**"),
        content_scan_safety_tokens=("STRIPE_SECRET",),
        content_scan_safety_patterns=(),
        backend_path_roots=("src/",),
    )

    # billing path -> high_stakes under the custom config
    assert classify(["src/billing/charge.py"], "code", cfg).tier == TIER_HIGH_STAKES

    # a path that is high-stakes for TradeWatcher is just backend here
    r = classify(["backend/app/promotion_gate.py"], "code", cfg)
    # backend/app/ is not under this config's backend roots ("src/") and matches
    # no high-stakes pattern -> unknown-path default = high_stakes (deny-first).
    assert r.tier == TIER_HIGH_STAKES
    assert r.reason == "unknown_path_default"


def test_custom_config_content_token_scan():
    cfg = RepoConfig(
        high_stakes_paths=(),
        content_scan_safety_tokens=("STRIPE_SECRET",),
        content_scan_safety_patterns=(),
        backend_path_roots=("src/",),
    )
    # diff mentions the custom token -> high_stakes regardless of path
    r = classify(["README.md"], "added STRIPE_SECRET = ...", cfg)
    assert r.tier == TIER_HIGH_STAKES
    assert r.reason == "content_scan_safety_match"


def test_custom_config_backend_root():
    cfg = RepoConfig(
        high_stakes_paths=(),
        content_scan_safety_tokens=(),
        content_scan_safety_patterns=(),
        backend_path_roots=("src/",),
    )
    # src/ file, no high-stakes match -> backend tier
    r = classify(["src/util/helpers.py"], "code", cfg)
    assert r.tier == TIER_BACKEND


def test_none_config_equals_default_config():
    # Passing the default RepoConfig must classify identically to passing None.
    files = ["backend/app/promotion_gate.py"]
    assert (
        classify(files, "code", None).tier
        == classify(files, "code", RepoConfig()).tier
        == TIER_HIGH_STAKES
    )
