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


def test_new_cut1_fields_load_from_config(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "head_lock_paths": ["src/payments/**"],
        "escalation_cooldown_minutes": 3,
        "project_description": "a payments app",
        "review_guidance": ["PCI scope", "idempotency"],
    }), encoding="utf-8")
    cfg = RC.load_repo_config(p)
    assert cfg.head_lock_paths == ("src/payments/**",)
    assert cfg.escalation_cooldown_minutes == 3
    assert cfg.project_description == "a payments app"
    assert cfg.review_guidance == ("PCI scope", "idempotency")


def test_generic_defaults_carry_no_stocktrader_rules():
    # The genericization contract: dispatcher defaults must NOT carry any one
    # project's specific rules. StockTrader's live in its .peer-review.json.
    cfg = RC.RepoConfig()
    assert cfg.project_name == ""
    assert cfg.head_lock_paths == ()
    assert cfg.review_guidance == ()
    assert "paper_only" not in cfg.content_scan_safety_tokens
    assert not any("schwab" in pat for pat in cfg.high_stakes_paths)
    assert "backend/app/" not in cfg.backend_path_roots


def test_resolve_repo_config_prefers_first_existing(tmp_path):
    primary = tmp_path / "primary.json"
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"project_name": "FromLegacy"}), encoding="utf-8")
    # primary absent -> legacy used
    cfg = RC.resolve_repo_config(primary, legacy)
    assert cfg.project_name == "FromLegacy"
    # primary present -> primary wins
    primary.write_text(json.dumps({"project_name": "FromPrimary"}), encoding="utf-8")
    cfg2 = RC.resolve_repo_config(primary, legacy)
    assert cfg2.project_name == "FromPrimary"
    # neither -> generic defaults
    assert RC.resolve_repo_config(tmp_path / "a.json", tmp_path / "b.json") == RC.RepoConfig()


def test_grok_model_roundtrips_and_grok_roster_accepted(tmp_path):
    # grok_model is a known string field; "grok" is a valid roster member so a
    # tenant can opt grok into the high_stakes panel via committed config.
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "grok_model": "grok-4-fast",
        "high_stakes_reviewers": ["claude", "gpt", "gemini", "grok"],
    }), encoding="utf-8")
    cfg = RC.load_repo_config(p)
    assert cfg.grok_model == "grok-4-fast"
    assert cfg.high_stakes_reviewers == ("claude", "gpt", "gemini", "grok")
    assert cfg.to_dict()["grok_model"] == "grok-4-fast"


def test_grok_not_in_default_roster():
    # Opt-in: grok must NOT be in any default roster, so an existing tenant
    # without XAI_API_KEY never escalates on a missing grok verdict.
    cfg = RC.RepoConfig()
    assert "grok" not in cfg.high_stakes_reviewers
    assert "grok" not in cfg.routine_reviewers
    assert "grok" not in cfg.backend_reviewers


def test_repo_context_defaults_and_override(tmp_path):
    cfg = RC.RepoConfig()
    assert cfg.repo_context_enabled is True
    assert cfg.repo_context_budget_chars == 30000
    p = tmp_path / "c.json"
    p.write_text(json.dumps(
        {"repo_context_enabled": False, "repo_context_budget_chars": 5000}
    ), encoding="utf-8")
    loaded = RC.load_repo_config(p)
    assert loaded.repo_context_enabled is False
    assert loaded.repo_context_budget_chars == 5000
    assert loaded.to_dict()["repo_context_enabled"] is False


def test_repo_context_enabled_rejects_non_bool(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"repo_context_enabled": "yes"}), encoding="utf-8")
    with pytest.raises(ValueError):
        RC.load_repo_config(p)
