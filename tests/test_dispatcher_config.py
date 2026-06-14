"""Tests for scripts.dispatcher.config.load_from_env.

Covers GPT review #3 on PR #79: the whitespace-stripping fix was one of the
two live-smoke root causes (a trailing newline in a stored secret produces an
"Invalid header value" error). This proves every secret env var is stripped
and that empty-after-strip becomes None.
"""

from scripts.dispatcher.config import load_from_env


SECRET_VARS = [
    ("ANTHROPIC_API_KEY", "anthropic_api_key"),
    ("OPENAI_API_KEY", "openai_api_key"),
    ("GEMINI_API_KEY", "gemini_api_key"),
    ("XAI_API_KEY", "xai_api_key"),
    ("RESEND_API_KEY", "resend_api_key"),
    ("GITHUB_TOKEN", "github_token"),
    ("DISPATCHER_VERDICT_SECRET", "verdict_secret"),
]


def test_all_secrets_stripped_of_trailing_newline():
    # Each secret carries the exact failure shape from the smoke test: a
    # trailing newline. load_from_env must strip it.
    env = {
        "GITHUB_REPOSITORY": "NFS-247/StockTrader",
    }
    for var, _ in SECRET_VARS:
        env[var] = f"value-for-{var}\n"

    cfg = load_from_env(env)

    for var, attr in SECRET_VARS:
        assert getattr(cfg, attr) == f"value-for-{var}"
        assert "\n" not in (getattr(cfg, attr) or "")


def test_secrets_stripped_of_surrounding_spaces():
    env = {"ANTHROPIC_API_KEY": "  spaced-key  ", "GITHUB_TOKEN": "tok"}
    cfg = load_from_env(env)
    assert cfg.anthropic_api_key == "spaced-key"


def test_empty_after_strip_becomes_none():
    env = {
        "ANTHROPIC_API_KEY": "   ",   # whitespace only -> None
        "OPENAI_API_KEY": "\n",        # newline only -> None
        "GITHUB_TOKEN": "real-token",
    }
    cfg = load_from_env(env)
    assert cfg.anthropic_api_key is None
    assert cfg.openai_api_key is None
    assert cfg.github_token == "real-token"


def test_missing_secret_is_none():
    env = {"GITHUB_TOKEN": "tok"}  # only github token present
    cfg = load_from_env(env)
    assert cfg.anthropic_api_key is None
    assert cfg.gemini_api_key is None
    assert cfg.resend_api_key is None
    assert cfg.verdict_secret is None


def test_operator_email_stripped():
    env = {"OPERATOR_EMAIL": "  nick@example.com\n", "GITHUB_TOKEN": "tok"}
    cfg = load_from_env(env)
    assert cfg.operator_email == "nick@example.com"


# ---- Phase 3 A3: project metadata + rosters from repo config file ----------

import json as _json
from scripts.dispatcher.config import (
    tiers_from_repo_config,
    REPO_CONFIG_PATH_ENV,
    REPO_CONFIG_LEGACY_PATH_ENV,
)
from scripts.dispatcher.repo_config import RepoConfig


def test_no_config_file_uses_generic_defaults(tmp_path):
    # Point at a nonexistent path so a real .peer-review.json in the CWD (this
    # repo now ships one for self-review) can't bleed into the "no config" case.
    cfg = load_from_env({"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "NFS-247/Canary",
                         REPO_CONFIG_PATH_ENV: str(tmp_path / "nope.json")})
    # project_name falls back to the repo name when no config supplies one.
    assert cfg.project_name == "Canary"
    assert cfg.operator_github_login == "NERT24"
    assert cfg.tiers["high_stakes"].reviewers == ("claude", "gpt", "gemini")
    assert cfg.tiers["backend"].reviewers == ("claude", "gpt")
    assert cfg.per_pr_cost_ceiling_usd == 5.0
    assert cfg.daily_cost_ceiling_usd == 20.0
    assert cfg.escalation_cooldown_minutes == 10


def test_config_file_supplies_project_metadata(tmp_path):
    p = tmp_path / "ai-peer-review.json"
    p.write_text(_json.dumps({
        "project_name": "Vendor Intelligence",
        "operator_github_login": "someone-else",
        "high_stakes_reviewers": ["claude", "gpt"],
        "per_pr_cost_ceiling_usd": 3,
        "daily_cost_ceiling_usd": 12,
    }), encoding="utf-8")

    cfg = load_from_env({"GITHUB_TOKEN": "t", REPO_CONFIG_PATH_ENV: str(p)})
    assert cfg.project_name == "Vendor Intelligence"
    assert cfg.operator_github_login == "someone-else"
    assert cfg.tiers["high_stakes"].reviewers == ("claude", "gpt")
    assert cfg.per_pr_cost_ceiling_usd == 3.0
    assert cfg.daily_cost_ceiling_usd == 12.0
    # repo_config is carried through for classify()
    assert cfg.repo_config.project_name == "Vendor Intelligence"


def test_env_var_overrides_config_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(_json.dumps({"project_name": "FromFile"}), encoding="utf-8")
    cfg = load_from_env({
        "GITHUB_TOKEN": "t",
        REPO_CONFIG_PATH_ENV: str(p),
        "PROJECT_NAME": "FromEnv",
    })
    # env wins over file
    assert cfg.project_name == "FromEnv"


def test_legacy_config_path_used_when_primary_absent(tmp_path):
    # A repo still carrying .github/ai-peer-review.json keeps working: when the
    # primary .peer-review.json is absent, the legacy path is loaded.
    legacy = tmp_path / "ai-peer-review.json"
    legacy.write_text(_json.dumps({"project_name": "LegacyProj"}), encoding="utf-8")
    cfg = load_from_env({
        "GITHUB_TOKEN": "t",
        REPO_CONFIG_PATH_ENV: str(tmp_path / "nope.json"),  # primary absent
        REPO_CONFIG_LEGACY_PATH_ENV: str(legacy),
    })
    assert cfg.project_name == "LegacyProj"


def test_cooldown_env_override_zero_disables():
    cfg = load_from_env({"GITHUB_TOKEN": "t", "ESCALATION_COOLDOWN_MINUTES": "0"})
    assert cfg.escalation_cooldown_minutes == 0


def test_model_selection_defaults_empty(tmp_path):
    # Isolate from any real .peer-review.json in the CWD (see note above).
    cfg = load_from_env({"GITHUB_TOKEN": "t", REPO_CONFIG_PATH_ENV: str(tmp_path / "nope.json")})
    assert cfg.claude_model == ""
    assert cfg.gpt_model == ""
    assert cfg.gemini_model == ""


def test_model_selection_from_config_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(_json.dumps({
        "claude_model": "claude-sonnet-4-6",
        "gemini_model": "gemini-2.5-flash",
    }), encoding="utf-8")
    cfg = load_from_env({"GITHUB_TOKEN": "t", REPO_CONFIG_PATH_ENV: str(p)})
    assert cfg.claude_model == "claude-sonnet-4-6"
    assert cfg.gemini_model == "gemini-2.5-flash"
    assert cfg.gpt_model == ""  # unset -> client default


def test_model_env_var_overrides_config_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(_json.dumps({"gemini_model": "gemini-2.5-flash"}), encoding="utf-8")
    cfg = load_from_env({
        "GITHUB_TOKEN": "t",
        REPO_CONFIG_PATH_ENV: str(p),
        "GEMINI_MODEL": "gemini-2.5-pro",  # operator override wins
    })
    assert cfg.gemini_model == "gemini-2.5-pro"


def test_build_client_applies_configured_model(tmp_path):
    # End-to-end: a model pinned in config reaches the actual client instance,
    # so the real API call (and its pricing) uses the cheaper model.
    from scripts.dispatcher.main import _build_client

    p = tmp_path / "c.json"
    p.write_text(_json.dumps({"gemini_model": "gemini-2.5-flash"}), encoding="utf-8")
    cfg = load_from_env({
        "GITHUB_TOKEN": "t",
        "GEMINI_API_KEY": "g",
        REPO_CONFIG_PATH_ENV: str(p),
    })
    client = _build_client("gemini", cfg)
    assert client is not None
    assert client._model == "gemini-2.5-flash"


def test_tiers_from_repo_config_maps_rosters():
    rc = RepoConfig(
        routine_reviewers=("claude",),
        backend_reviewers=("claude", "gpt"),
        high_stakes_reviewers=("claude", "gpt", "gemini"),
        routine_round_budget=4,
        high_stakes_round_budget=1,
    )
    tiers = tiers_from_repo_config(rc)
    assert tiers["routine"].reviewers == ("claude",)
    assert tiers["routine"].round_budget == 4
    assert tiers["high_stakes"].round_budget == 1


def test_xai_key_and_grok_model_load():
    # XAI_API_KEY loads like the other secrets; GROK_MODEL resolves via env.
    env = {"GITHUB_TOKEN": "tok", "XAI_API_KEY": "  xk \n", "GROK_MODEL": " grok-4-fast "}
    cfg = load_from_env(env)
    assert cfg.xai_api_key == "xk"
    assert cfg.grok_model == "grok-4-fast"


def test_xai_key_absent_is_none_and_grok_not_default():
    # Opt-in posture: no key -> None, and grok is in no default tier roster.
    cfg = load_from_env({"GITHUB_TOKEN": "tok"})
    assert cfg.xai_api_key is None
    for tier in cfg.tiers.values():
        assert "grok" not in tier.reviewers
