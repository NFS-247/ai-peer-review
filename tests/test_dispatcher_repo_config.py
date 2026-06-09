"""Tests for scripts.dispatcher.repo_config (Phase 3 A1).

The loader must:
- default to exactly the current TradeWatcher hardcoded values (so a repo with
  no config file behaves as StockTrader does today),
- overlay only the fields present in a config file,
- ignore unknown keys (forward compatible),
- fail loud on malformed values (so a bad config can't silently misclassify).
"""

import json

import pytest

from scripts.dispatcher import classify as C
from scripts.dispatcher import repo_config as RC


def test_defaults_match_current_classify_constants():
    cfg = RC.RepoConfig()
    assert cfg.high_stakes_paths == C.HIGH_STAKES_PATH_PATTERNS
    assert cfg.content_scan_safety_tokens == C.CONTENT_SAFETY_TOKENS
    assert cfg.content_scan_safety_patterns == C.CONTENT_SAFETY_PATTERNS
    assert cfg.routine_root_exact == C.ROUTINE_ROOT_EXACT
    assert cfg.routine_root_globs == C.ROUTINE_ROOT_GLOBS
    assert cfg.routine_path_patterns == C.ROUTINE_PATH_PATTERNS
    assert cfg.backend_path_roots == C.BACKEND_PATH_ROOTS


def test_defaults_match_current_rosters_and_ceilings():
    cfg = RC.RepoConfig()
    assert cfg.routine_reviewers == ("claude", "gpt")
    assert cfg.backend_reviewers == ("claude", "gpt")
    assert cfg.high_stakes_reviewers == ("claude", "gpt", "gemini")
    assert cfg.max_review_rounds == 6
    assert cfg.high_stakes_round_budget == 2
    assert cfg.backend_round_budget == 3
    assert cfg.per_pr_cost_ceiling_usd == 5.0
    assert cfg.daily_cost_ceiling_usd == 20.0


def test_missing_file_returns_defaults(tmp_path):
    cfg = RC.load_repo_config(tmp_path / "nope.json")
    assert cfg == RC.RepoConfig()


def test_partial_override_only_changes_named_fields(tmp_path):
    p = tmp_path / "ai-peer-review.json"
    p.write_text(json.dumps({
        "project_name": "Vendor Intelligence",
        "high_stakes_paths": ["src/billing/**", "src/auth/**"],
    }), encoding="utf-8")

    cfg = RC.load_repo_config(p)
    assert cfg.project_name == "Vendor Intelligence"
    assert cfg.high_stakes_paths == ("src/billing/**", "src/auth/**")
    # Everything else stays default.
    assert cfg.operator_github_login == "NERT24"
    assert cfg.high_stakes_reviewers == ("claude", "gpt", "gemini")
    assert cfg.content_scan_safety_tokens == C.CONTENT_SAFETY_TOKENS


def test_unknown_keys_ignored(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"project_name": "X", "future_field": 123}), encoding="utf-8")
    cfg = RC.load_repo_config(p)
    assert cfg.project_name == "X"


def test_rosters_and_ceilings_override(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "high_stakes_reviewers": ["claude", "gpt"],
        "per_pr_cost_ceiling_usd": 2.5,
        "daily_cost_ceiling_usd": 10,
        "high_stakes_round_budget": 1,
    }), encoding="utf-8")
    cfg = RC.load_repo_config(p)
    assert cfg.high_stakes_reviewers == ("claude", "gpt")
    assert cfg.per_pr_cost_ceiling_usd == 2.5
    assert cfg.daily_cost_ceiling_usd == 10.0
    assert cfg.high_stakes_round_budget == 1


def test_malformed_json_fails_loud(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError):
        RC.load_repo_config(p)


def test_non_object_json_fails_loud(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    with pytest.raises(ValueError):
        RC.load_repo_config(p)


def test_wrong_typed_field_fails_loud(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"high_stakes_paths": "should-be-a-list"}), encoding="utf-8")
    with pytest.raises(ValueError):
        RC.load_repo_config(p)


def test_wrong_typed_scalar_fails_loud(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"per_pr_cost_ceiling_usd": "free"}), encoding="utf-8")
    with pytest.raises(ValueError):
        RC.load_repo_config(p)


def test_bool_rejected_for_int_field(tmp_path):
    # bool is a subclass of int in Python; the loader must reject it.
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"max_review_rounds": True}), encoding="utf-8")
    with pytest.raises(ValueError):
        RC.load_repo_config(p)


def test_to_dict_roundtrips_through_from_mapping():
    cfg = RC.RepoConfig(project_name="RoundTrip", high_stakes_reviewers=("claude",))
    cfg2 = RC.from_mapping(cfg.to_dict())
    assert cfg2 == cfg
