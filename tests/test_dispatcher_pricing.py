"""Tests for scripts.dispatcher.pricing (per-model token pricing).

This is the fix for the false daily-ceiling trips: spend must be priced at the
model ACTUALLY in use, with an env override for exact rates — not a single fixed
rate for the most expensive default model.
"""

from scripts.dispatcher.pricing import cost_for, resolve_prices


def test_exact_model_match():
    assert resolve_prices("claude", "claude-opus-4") == (15.00, 75.00)
    assert resolve_prices("gpt", "gpt-5") == (1.25, 10.00)
    assert resolve_prices("gemini", "gemini-2.5-pro") == (1.25, 10.00)


def test_family_prefix_match_for_dated_model_ids():
    # The real default ids carry a suffix; they must resolve to the family.
    assert resolve_prices("claude", "claude-opus-4-7") == (15.00, 75.00)
    assert resolve_prices("claude", "claude-sonnet-4-6") == (3.00, 15.00)
    assert resolve_prices("gemini", "gemini-2.5-flash-002") == (0.30, 2.50)


def test_longest_prefix_wins_over_shorter_family():
    # 'gpt-5-mini' must NOT collapse to the pricier 'gpt-5' family.
    assert resolve_prices("gpt", "gpt-5-mini") == (0.25, 2.00)
    assert resolve_prices("gpt", "gpt-5-nano") == (0.05, 0.40)
    # A cheaper Claude/Gemini variant resolves to its own (cheaper) rate.
    assert resolve_prices("claude", "claude-haiku-4-5") == (1.00, 5.00)
    assert resolve_prices("gemini", "gemini-2.5-flash-lite") == (0.10, 0.40)


def test_base_family_not_shadowed_by_more_specific_sibling():
    # 'gpt-5' must stay 'gpt-5' even though 'gpt-5-mini' exists.
    assert resolve_prices("gpt", "gpt-5") == (1.25, 10.00)
    assert resolve_prices("gemini", "gemini-2.5-flash") == (0.30, 2.50)


def test_unknown_model_falls_back_to_provider_default():
    assert resolve_prices("claude", "claude-something-new") == (15.00, 75.00)
    assert resolve_prices("gpt", "totally-unknown") == (1.25, 10.00)
    assert resolve_prices("gemini", "gemini-9-ultra") == (1.25, 10.00)


def test_env_override_wins_over_table():
    env = {"ANTHROPIC_INPUT_PRICE_PER_M": "2.0", "ANTHROPIC_OUTPUT_PRICE_PER_M": "9.0"}
    assert resolve_prices("claude", "claude-opus-4-7", env=env) == (2.0, 9.0)


def test_env_override_each_side_independently():
    # Only input overridden -> output stays at the table rate.
    env = {"OPENAI_INPUT_PRICE_PER_M": "0.50"}
    assert resolve_prices("gpt", "gpt-5", env=env) == (0.50, 10.00)


def test_malformed_env_override_falls_back_not_crash():
    env = {"GEMINI_INPUT_PRICE_PER_M": "free", "GEMINI_OUTPUT_PRICE_PER_M": ""}
    # Bad/empty values must not crash a review round; use the table rate.
    assert resolve_prices("gemini", "gemini-2.5-pro", env=env) == (1.25, 10.00)


def test_negative_env_override_clamped_to_zero():
    env = {"OPENAI_INPUT_PRICE_PER_M": "-5"}
    assert resolve_prices("gpt", "gpt-5", env=env)[0] == 0.0


def test_cost_for_uses_resolved_price():
    # 1M input + 1M output at opus rate = 15 + 75 = 90.
    assert cost_for("claude", "claude-opus-4-7", 1_000_000, 1_000_000) == 90.0
    # Same tokens on a cheap model cost far less — the whole point of the fix.
    cheap = cost_for("gpt", "gpt-5-nano", 1_000_000, 1_000_000)
    assert cheap == round(0.05 + 0.40, 6)
    assert cheap < cost_for("gpt", "gpt-5", 1_000_000, 1_000_000)


def test_cost_for_respects_env_override():
    env = {"ANTHROPIC_INPUT_PRICE_PER_M": "0", "ANTHROPIC_OUTPUT_PRICE_PER_M": "0"}
    assert cost_for("claude", "claude-opus-4-7", 5_000, 5_000, env=env) == 0.0
