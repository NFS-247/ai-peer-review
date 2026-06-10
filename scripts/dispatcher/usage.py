"""Per-tenant AI usage metering and billing math.

This is the foundation for billing each tenant for the AI it consumes. It is
PURE (no I/O); the durable per-tenant ledger lives in ``usage_ledger.py`` and
the actual invoicing (Stripe, etc.) is a separate service that reads that
ledger. The dispatcher's job here is to emit accurate, attributed usage.

Two billing modes per tenant:

- ``platform``: the platform (NFS) supplies the API keys and pays the providers,
  then bills the tenant for usage with a markup. usage charge = cost * markup.
- ``byok``    : the tenant brings their own keys and pays the providers
  directly, so the platform's *usage* charge is 0 — it only takes a separate
  flat dev/service fee, applied per billing period at invoice time.

A flat ``dev_fee_usd`` is a per-period charge the invoicing layer adds on top;
it is stored in config and surfaced here, not computed per call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


BILLING_MODE_PLATFORM = "platform"
BILLING_MODE_BYOK = "byok"
VALID_BILLING_MODES = frozenset({BILLING_MODE_PLATFORM, BILLING_MODE_BYOK})


def normalize_billing_mode(mode: str) -> str:
    """Coerce to a valid mode; anything unrecognized defaults to byok (safe:
    the platform bills nothing for usage it didn't pay for)."""
    m = (mode or "").strip().lower()
    return m if m in VALID_BILLING_MODES else BILLING_MODE_BYOK


@dataclass(frozen=True)
class UsageEvent:
    """One metered AI call (one reviewer, one round)."""

    reviewer: str  # "claude" / "gpt" / "gemini"
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float  # provider cost — what NFS pays (platform) or the tenant pays (byok)

    def to_dict(self) -> dict:
        return {
            "reviewer": self.reviewer,
            "model": self.model,
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "cost_usd": round(self.cost_usd, 6),
        }

    @staticmethod
    def from_dict(d: dict) -> "UsageEvent":
        return UsageEvent(
            reviewer=str(d.get("reviewer", "")),
            model=str(d.get("model", "")),
            input_tokens=int(d.get("input_tokens", 0)),
            output_tokens=int(d.get("output_tokens", 0)),
            cost_usd=float(d.get("cost_usd", 0.0)),
        )


def compute_usage_charge(
    cost_usd: float,
    *,
    billing_mode: str,
    markup_multiplier: float,
) -> float:
    """What the platform bills a tenant for a given provider cost.

    - platform: the platform paid the provider → bills ``cost * markup``
      (markup >= 1.0 covers cost + margin; e.g. 1.3 == 30% margin).
    - byok    : the tenant paid the provider directly → platform usage charge 0.

    Pure. The flat dev fee is added per-period at invoice time, not here.
    """
    if cost_usd < 0:
        cost_usd = 0.0
    if normalize_billing_mode(billing_mode) == BILLING_MODE_PLATFORM:
        return round(cost_usd * max(markup_multiplier, 0.0), 6)
    return 0.0


def billable_amount(
    usage_cost_usd: float,
    *,
    billing_mode: str,
    markup_multiplier: float,
    dev_fee_usd: float,
) -> float:
    """Total a tenant owes for a billing period: usage charge + flat dev fee.

    platform → cost*markup + dev_fee.  byok → dev_fee only (AI cost is theirs).
    """
    charge = compute_usage_charge(
        usage_cost_usd, billing_mode=billing_mode, markup_multiplier=markup_multiplier
    )
    return round(charge + max(dev_fee_usd, 0.0), 6)


@dataclass
class UsageSummary:
    """Aggregate usage for a tenant (bounded — totals + per-provider breakdown)."""

    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    calls: int = 0
    by_provider: dict = field(default_factory=dict)  # reviewer -> {cost_usd,in,out,calls}

    def add(self, ev: UsageEvent) -> None:
        self.total_cost_usd = round(self.total_cost_usd + ev.cost_usd, 6)
        self.total_input_tokens += int(ev.input_tokens)
        self.total_output_tokens += int(ev.output_tokens)
        self.calls += 1
        p = self.by_provider.setdefault(
            ev.reviewer,
            {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0},
        )
        p["cost_usd"] = round(p["cost_usd"] + ev.cost_usd, 6)
        p["input_tokens"] += int(ev.input_tokens)
        p["output_tokens"] += int(ev.output_tokens)
        p["calls"] += 1

    def to_dict(self) -> dict:
        return {
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "calls": self.calls,
            "by_provider": self.by_provider,
        }

    @staticmethod
    def from_dict(d: dict) -> "UsageSummary":
        s = UsageSummary(
            total_cost_usd=float(d.get("total_cost_usd", 0.0)),
            total_input_tokens=int(d.get("total_input_tokens", 0)),
            total_output_tokens=int(d.get("total_output_tokens", 0)),
            calls=int(d.get("calls", 0)),
            by_provider=dict(d.get("by_provider", {}) or {}),
        )
        return s


def summarize(events: Iterable[UsageEvent]) -> UsageSummary:
    s = UsageSummary()
    for ev in events:
        s.add(ev)
    return s


__all__ = [
    "BILLING_MODE_PLATFORM",
    "BILLING_MODE_BYOK",
    "VALID_BILLING_MODES",
    "normalize_billing_mode",
    "UsageEvent",
    "UsageSummary",
    "compute_usage_charge",
    "billable_amount",
    "summarize",
]
