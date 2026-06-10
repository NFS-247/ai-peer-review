"""Durable per-tenant usage & billing ledger.

Where ``global_state.py`` keeps a 24h rolling *spend* total for the safety
ceiling, this keeps a persistent, attributed *billing* record per tenant: how
much AI each repo has consumed, broken down by provider, plus the platform's
running usage charge under the tenant's billing mode/markup.

Storage mirrors the rest of the dispatcher: a single dedicated tracking issue
per repo (``issues: write``, no ``contents: write``). The billable fields are
HMAC-signed with the repo's verdict secret so a tenant with repo-write can't
silently understate usage. Bounded by design: running aggregates (fixed size)
plus a capped tail of recent events for spot-audit.

NOTE (production): the authoritative billing record should live in an
NFS-controlled datastore, not the tenant's own repo. This ledger is the
foundation — the dispatcher *emits* accurate, signed usage; a billing service
collects/period-snapshots it and issues invoices (Stripe, etc.). That
collector + payment integration is deliberately out of scope here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Optional, Sequence

from . import usage
from .github_api import GitHubAPI

USAGE_LEDGER_MARKER = "<!-- tradewatcher-dispatcher-usage-ledger -->"
USAGE_LEDGER_TITLE = "[dispatcher] usage & billing ledger (do not close)"
MAX_RECENT_EVENTS = 250

_LEDGER_RE = re.compile(r"```tradewatcher-usage-ledger\s*\n(.*?)\n```", re.DOTALL)


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _empty_ledger() -> dict:
    return {
        "summary": usage.UsageSummary().to_dict(),
        "billing": {"mode": usage.BILLING_MODE_BYOK, "markup_multiplier": 1.0, "dev_fee_usd": 0.0},
        "usage_charge_usd": 0.0,
        "billable_usd": 0.0,
        "recent_events": [],
    }


def _signing_string(ledger: dict) -> str:
    """Canonical string over the billable-relevant fields (not the audit tail)."""
    payload = {
        "summary": ledger.get("summary", {}),
        "billing": ledger.get("billing", {}),
        "usage_charge_usd": ledger.get("usage_charge_usd", 0.0),
        "billable_usd": ledger.get("billable_usd", 0.0),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sign(ledger: dict, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), _signing_string(ledger).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def apply_events(
    ledger: dict,
    events: Sequence[usage.UsageEvent],
    *,
    billing_mode: str,
    markup_multiplier: float,
    dev_fee_usd: float,
    now_ts: float,
) -> dict:
    """Pure: fold ``events`` into ``ledger`` and recompute charges. Returns the
    updated ledger dict (no I/O, no signature)."""
    summary = usage.UsageSummary.from_dict(ledger.get("summary", {}))
    recent = list(ledger.get("recent_events", []))
    for ev in events:
        summary.add(ev)
        rec = ev.to_dict()
        rec["ts"] = now_ts
        recent.append(rec)
    recent = recent[-MAX_RECENT_EVENTS:]

    mode = usage.normalize_billing_mode(billing_mode)
    usage_charge = usage.compute_usage_charge(
        summary.total_cost_usd, billing_mode=mode, markup_multiplier=markup_multiplier
    )
    billable = usage.billable_amount(
        summary.total_cost_usd,
        billing_mode=mode,
        markup_multiplier=markup_multiplier,
        dev_fee_usd=dev_fee_usd,
    )
    return {
        "summary": summary.to_dict(),
        "billing": {
            "mode": mode,
            "markup_multiplier": float(markup_multiplier),
            "dev_fee_usd": float(dev_fee_usd),
        },
        "usage_charge_usd": usage_charge,
        "billable_usd": billable,
        "recent_events": recent,
    }


def _render(ledger: dict, secret: str) -> str:
    body = dict(ledger)
    if secret:
        body["signature"] = _sign(ledger, secret)
    return (
        f"{USAGE_LEDGER_MARKER}\n\n"
        "Dispatcher per-tenant usage & billing ledger. Managed automatically. "
        "Do not edit or close.\n\n"
        "```tradewatcher-usage-ledger\n"
        f"{json.dumps(body, indent=2)}\n"
        "```\n"
    )


def _parse(body: str, secret: str) -> Optional[dict]:
    m = _LEDGER_RE.search(body or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    sig = data.pop("signature", None)
    if secret:
        # Tamper-evidence: a present-but-wrong signature is rejected (start
        # fresh) so a doctored ledger can't pass as authoritative.
        if not sig or not hmac.compare_digest(_sign(data, secret), str(sig)):
            return None
    return data


def record(
    api: GitHubAPI,
    events: Sequence[usage.UsageEvent],
    *,
    billing_mode: str,
    markup_multiplier: float,
    dev_fee_usd: float,
    secret: str,
    now_ts: Optional[float] = None,
) -> dict:
    """Append ``events`` to the tenant's ledger, persist, and return it.

    Best-effort by contract: callers wrap this so a billing-ledger hiccup never
    breaks a review. Creates the tracking issue on first use.
    """
    now = now_ts if now_ts is not None else _now_ts()
    issue = api.find_issue_by_marker(USAGE_LEDGER_MARKER)
    base = _parse(issue.get("body") or "", secret) if issue else None
    if base is None:
        base = _empty_ledger()

    updated = apply_events(
        base,
        events,
        billing_mode=billing_mode,
        markup_multiplier=markup_multiplier,
        dev_fee_usd=dev_fee_usd,
        now_ts=now,
    )

    rendered = _render(updated, secret)
    if issue is None:
        api.create_issue(USAGE_LEDGER_TITLE, rendered)
    else:
        api.update_issue_body(int(issue["number"]), rendered)
    return updated


def read(api: GitHubAPI, secret: str) -> dict:
    """Read the current ledger (verified). Returns an empty ledger if none/invalid."""
    issue = api.find_issue_by_marker(USAGE_LEDGER_MARKER)
    if not issue:
        return _empty_ledger()
    return _parse(issue.get("body") or "", secret) or _empty_ledger()


__all__ = [
    "USAGE_LEDGER_MARKER",
    "USAGE_LEDGER_TITLE",
    "MAX_RECENT_EVENTS",
    "apply_events",
    "record",
    "read",
]
