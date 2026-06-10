"""Global (cross-PR) 24-hour spend ledger.

Implements the design's Section 5 daily spend ceiling: total dispatcher spend
across ALL PRs in any 24-hour rolling window. This is distinct from the
per-PR cost ceiling (which lives in CrossRunState).

Storage: a single dedicated tracking issue, identified by a marker in its
body, holding a JSON list of {ts, cost} events. The dispatcher has
``issues: write`` (no ``contents: write``), so an issue is the right place
for cross-PR durable state. The ledger is pruned to the last 24h on each
write so it cannot grow unbounded.

The window math is a pure function (``sum_recent``) and is unit-tested
without any GitHub calls.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

from .github_api import GitHubAPI


GLOBAL_STATE_MARKER = "<!-- tradewatcher-dispatcher-global-spend -->"
GLOBAL_STATE_TITLE = "[dispatcher] global spend ledger (do not close)"
WINDOW_SECONDS = 24 * 60 * 60

_LEDGER_RE = re.compile(
    r"```tradewatcher-global-spend\s*\n(.*?)\n```",
    re.DOTALL,
)


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _clean_by(by: object) -> dict:
    """Coerce a per-provider cost map to {str: float}, dropping bad entries."""
    if not isinstance(by, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in by.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def sum_recent(events: list[dict], *, now_ts: float, window_seconds: int = WINDOW_SECONDS) -> float:
    """Sum the cost of events within the trailing window. Pure function."""
    cutoff = now_ts - window_seconds
    total = 0.0
    for ev in events:
        try:
            ts = float(ev.get("ts", 0))
            cost = float(ev.get("cost", 0))
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            total += cost
    return round(total, 6)


def sum_recent_by_provider(
    events: list[dict], *, now_ts: float, window_seconds: int = WINDOW_SECONDS
) -> dict[str, float]:
    """Per-provider spend within the trailing window. Pure function.

    Only events carrying a ``by`` map contribute to the breakdown; older
    events (cost-only) still count toward ``sum_recent`` but not here, so the
    breakdown converges as new per-provider events accrue. Lets the operator
    see WHICH model is driving spend instead of guessing.
    """
    cutoff = now_ts - window_seconds
    totals: dict[str, float] = {}
    for ev in events:
        try:
            ts = float(ev.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        for provider, amount in _clean_by(ev.get("by")).items():
            totals[provider] = round(totals.get(provider, 0.0) + amount, 6)
    return totals


def prune(events: list[dict], *, now_ts: float, window_seconds: int = WINDOW_SECONDS) -> list[dict]:
    """Drop events older than the window. Pure function. Preserves ``by`` maps."""
    cutoff = now_ts - window_seconds
    out = []
    for ev in events:
        try:
            ts = float(ev.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            kept = {"ts": ts, "cost": float(ev.get("cost", 0))}
            by = _clean_by(ev.get("by"))
            if by:
                kept["by"] = by
            out.append(kept)
    return out


def _render_issue_body(events: list[dict]) -> str:
    return (
        f"{GLOBAL_STATE_MARKER}\n\n"
        "Dispatcher global 24h spend ledger. Managed automatically. "
        "Do not edit or close.\n\n"
        "```tradewatcher-global-spend\n"
        f"{json.dumps({'events': events})}\n"
        "```\n"
    )


def _parse_events(body: str) -> list[dict]:
    match = _LEDGER_RE.search(body or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    events = data.get("events", [])
    return events if isinstance(events, list) else []


def _make_event(now: float, cost: float, by_provider: Optional[dict]) -> dict:
    ev = {"ts": now, "cost": float(cost)}
    by = _clean_by(by_provider)
    if by:
        ev["by"] = by
    return ev


def record_and_get_24h_total(
    api: GitHubAPI,
    added_cost_usd: float,
    *,
    by_provider: Optional[dict] = None,
    now_ts: Optional[float] = None,
) -> float:
    """Append a spend event, prune to 24h, persist, and return the 24h total.

    ``by_provider`` (optional) attributes this round's cost per reviewer
    (e.g. ``{"claude": 0.41, "gpt": 0.06}``) so the breakdown is available to
    the budget warning / ceiling escalation. Uses a dedicated tracking issue;
    if none exists yet, it is created.
    """
    now = now_ts if now_ts is not None else _now_ts()
    issue = api.find_issue_by_marker(GLOBAL_STATE_MARKER)

    if issue is None:
        events = [_make_event(now, added_cost_usd, by_provider)]
        api.create_issue(GLOBAL_STATE_TITLE, _render_issue_body(events))
        return sum_recent(events, now_ts=now)

    events = _parse_events(issue.get("body") or "")
    if added_cost_usd:
        events.append(_make_event(now, added_cost_usd, by_provider))
    events = prune(events, now_ts=now)
    api.update_issue_body(int(issue["number"]), _render_issue_body(events))
    return sum_recent(events, now_ts=now)


def get_24h_breakdown(
    api: GitHubAPI, *, now_ts: Optional[float] = None
) -> tuple[float, dict[str, float]]:
    """Read the 24h total AND its per-provider breakdown without recording."""
    now = now_ts if now_ts is not None else _now_ts()
    issue = api.find_issue_by_marker(GLOBAL_STATE_MARKER)
    if issue is None:
        return 0.0, {}
    events = _parse_events(issue.get("body") or "")
    return sum_recent(events, now_ts=now), sum_recent_by_provider(events, now_ts=now)


def budget_warning_due(
    *, total_before: float, total_after: float, ceiling: float, warn_fraction: float
) -> bool:
    """True only on the round that pushes 24h spend INTO the warning band.

    Pure. Fires exactly once on the crossing (``before`` below the threshold,
    ``after`` at/above it but still under the ceiling) — so it needs no
    persisted "already warned" flag. Returns False when the ceiling or warn
    fraction is disabled, or once spend is already over the hard ceiling (that
    case is the pause/escalation path, not a pre-warning).
    """
    if ceiling <= 0 or not (0.0 < warn_fraction < 1.0):
        return False
    threshold = ceiling * warn_fraction
    return total_before < threshold <= total_after < ceiling


def get_24h_total(api: GitHubAPI, *, now_ts: Optional[float] = None) -> float:
    """Read the current 24h spend total without recording anything."""
    now = now_ts if now_ts is not None else _now_ts()
    issue = api.find_issue_by_marker(GLOBAL_STATE_MARKER)
    if issue is None:
        return 0.0
    events = _parse_events(issue.get("body") or "")
    return sum_recent(events, now_ts=now)


__all__ = [
    "GLOBAL_STATE_MARKER",
    "GLOBAL_STATE_TITLE",
    "WINDOW_SECONDS",
    "sum_recent",
    "sum_recent_by_provider",
    "prune",
    "record_and_get_24h_total",
    "get_24h_total",
    "get_24h_breakdown",
    "budget_warning_due",
]
