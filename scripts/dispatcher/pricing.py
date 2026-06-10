"""Per-model token pricing for spend accounting.

Spend is metered as ``tokens × price-per-token`` and summed into the 24h ledger
that drives the daily cost ceiling. The price MUST track the model actually in
use — not a hard-coded default. Before this module each provider priced every
call at one fixed rate for its *default* (most expensive) model, so a tenant who
ran a cheaper model — or whose real diffs were small — watched the 24h ledger
massively overstate spend and trip the daily ceiling on PRs that had barely cost
anything. Here the price is resolved from the model name, with an env override
for exact contracted rates.

Prices are published list rates in USD per 1,000,000 tokens. They change; treat
the tables below as sensible defaults and set the env overrides to your actual
billed rates for an exactly-accurate ledger:

    ANTHROPIC_INPUT_PRICE_PER_M / ANTHROPIC_OUTPUT_PRICE_PER_M   (claude)
    OPENAI_INPUT_PRICE_PER_M    / OPENAI_OUTPUT_PRICE_PER_M       (gpt)
    GEMINI_INPUT_PRICE_PER_M    / GEMINI_OUTPUT_PRICE_PER_M       (gemini)

The model itself is selectable per provider (so a tenant can run a cheaper one)
via ANTHROPIC_MODEL / OPENAI_MODEL / GEMINI_MODEL; the price then follows it.

Resolution order for (provider, model):
  1. Env override        <PROVIDER>_INPUT_PRICE_PER_M / _OUTPUT_PRICE_PER_M
  2. Exact model match    in the provider's table
  3. Longest-family match (e.g. 'claude-opus-4' for 'claude-opus-4-7')
  4. The provider's default-model price (last resort)

Pure / stdlib-only and unit-tested without any network calls.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping, Optional


# provider key -> env-var prefix (matches each provider's *_API_KEY name).
_ENV_PREFIX = {"claude": "ANTHROPIC", "gpt": "OPENAI", "gemini": "GEMINI"}

# provider -> { model-or-family : (input_per_M, output_per_M) in USD }.
# Published list prices, early 2026. These are DEFAULTS; override via env for
# your exact contracted rates. Keys are matched exactly first, then as a family
# prefix ("<key>-..."), so a dated/suffixed model id resolves to its family.
_PRICE_TABLES: dict[str, dict[str, tuple[float, float]]] = {
    "claude": {
        "claude-opus-4": (15.00, 75.00),
        "claude-sonnet-4": (3.00, 15.00),
        "claude-haiku-4": (1.00, 5.00),
        "claude-3-5-sonnet": (3.00, 15.00),
        "claude-3-5-haiku": (0.80, 4.00),
        "claude-3-opus": (15.00, 75.00),
        "claude-3-sonnet": (3.00, 15.00),
        "claude-3-haiku": (0.25, 1.25),
    },
    "gpt": {
        "gpt-5": (1.25, 10.00),
        "gpt-5-mini": (0.25, 2.00),
        "gpt-5-nano": (0.05, 0.40),
        "gpt-4.1": (2.00, 8.00),
        "gpt-4.1-mini": (0.40, 1.60),
        "gpt-4.1-nano": (0.10, 0.40),
        "gpt-4o": (2.50, 10.00),
        "gpt-4o-mini": (0.15, 0.60),
        "o3": (2.00, 8.00),
        "o4-mini": (1.10, 4.40),
    },
    "gemini": {
        "gemini-2.5-pro": (1.25, 10.00),
        "gemini-2.5-flash": (0.30, 2.50),
        "gemini-2.5-flash-lite": (0.10, 0.40),
        "gemini-1.5-pro": (1.25, 5.00),
        "gemini-1.5-flash": (0.075, 0.30),
    },
}

# provider -> the table key whose price is the last-resort default (used when a
# model matches no exact key and no family prefix).
_DEFAULT_MODEL_KEY = {
    "claude": "claude-opus-4",
    "gpt": "gpt-5",
    "gemini": "gemini-2.5-pro",
}


def _table_lookup(table: Mapping[str, tuple[float, float]], model: str) -> Optional[tuple[float, float]]:
    """Exact match, else the longest family-prefix match ("<key>-...")."""
    if model in table:
        return table[model]
    best_key = ""
    best_price: Optional[tuple[float, float]] = None
    for key, price in table.items():
        if model.startswith(key + "-") and len(key) > len(best_key):
            best_key, best_price = key, price
    return best_price


def _parse_price(raw: Optional[str], fallback: float) -> float:
    """Parse an env price override; fall back (not crash) on a bad value.

    A malformed price env must never take down a review round — log and use the
    table value instead. Negative prices are clamped to 0.
    """
    if raw is None or raw.strip() == "":
        return fallback
    try:
        val = float(raw)
    except (TypeError, ValueError):
        print(f"pricing: ignoring non-numeric price override {raw!r}", file=sys.stderr)
        return fallback
    return val if val >= 0 else 0.0


def resolve_prices(
    provider: str, model: str, *, env: Optional[Mapping[str, str]] = None
) -> tuple[float, float]:
    """Return ``(input_per_M, output_per_M)`` USD for a (provider, model).

    Env overrides win over the table so the operator can pin exact rates; each
    side (input/output) overrides independently.
    """
    e = env if env is not None else os.environ
    table = _PRICE_TABLES.get(provider, {})
    default_key = _DEFAULT_MODEL_KEY.get(provider, "")
    base = (
        _table_lookup(table, model)
        or table.get(default_key)
        or (0.0, 0.0)
    )
    prefix = _ENV_PREFIX.get(provider, provider.upper())
    in_price = _parse_price(e.get(f"{prefix}_INPUT_PRICE_PER_M"), base[0])
    out_price = _parse_price(e.get(f"{prefix}_OUTPUT_PRICE_PER_M"), base[1])
    return (in_price, out_price)


def cost_for(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> float:
    """USD cost of one call: tokens × the resolved per-model price."""
    in_price, out_price = resolve_prices(provider, model, env=env)
    cost = (input_tokens * in_price / 1_000_000) + (output_tokens * out_price / 1_000_000)
    return round(cost, 6)


__all__ = [
    "resolve_prices",
    "cost_for",
]
