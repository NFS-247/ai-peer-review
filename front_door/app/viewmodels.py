"""Pure transforms: the engine's raw GitHub state -> UI view models.

This is the heart of the front door and the only part that must be exactly
right, so it is pure (no I/O) and exhaustively unit-tested. Everything the board
and inbox show is derived here from data the review engine already persists in
GitHub (labels, the signed PR state comment, the spend-ledger issue, and the
verdict blocks in PR comments).

The marker/label/fence constants mirror the engine. They are re-declared here
(not imported) on purpose: the front door is a separate, independently
deployable app — it must not depend on the engine package. If the engine ever
changes a marker, update it here too (there is a contract test for each).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional


# ---- engine contract constants (mirror scripts/dispatcher) -----------------
LABEL_PAUSED = "dispatcher:paused"
LABEL_ESCALATED = "dispatcher:escalated"
LABEL_READY = "dispatcher:ready-for-merge"
LABEL_SECRET_MISSING = "dispatcher:secret-missing"
TIER_PREFIX = "dispatcher:tier-"
ROUND_PREFIX = "dispatcher:round-"

STATE_MARKER = "<!-- tradewatcher-dispatcher-state -->"
GLOBAL_SPEND_MARKER = "<!-- tradewatcher-dispatcher-global-spend -->"
USAGE_LEDGER_MARKER = "<!-- tradewatcher-dispatcher-usage-ledger -->"

WINDOW_SECONDS = 24 * 60 * 60

# ---- statuses (UI vocabulary) ----------------------------------------------
STATUS_SECRET_MISSING = "secret-missing"
STATUS_PAUSED = "paused"
STATUS_ESCALATED = "escalated"
STATUS_READY = "ready"
STATUS_REVIEWING = "reviewing"
STATUS_UNKNOWN = "unknown"

# Actionable statuses surface in the approvals inbox.
ACTIONABLE_STATUSES = frozenset({STATUS_READY, STATUS_ESCALATED})

_VERDICT_BLOCK_RE = re.compile(r"```tradewatcher-verdict\s*\n(.*?)\n```", re.DOTALL)
# First fenced code block of any tag (used to pull the JSON out of the signed
# state comment and the ledger issues without coupling to the fence tag).
_FENCED_RE = re.compile(r"```[^\n]*\n(.*?)\n```", re.DOTALL)


@dataclass(frozen=True)
class BoardRow:
    repo: str
    number: int
    title: str
    url: str
    status: str
    tier: Optional[str]
    round: Optional[int]
    cost_usd: float
    reviewers: dict  # reviewer -> latest verdict ("approve"/"request_changes"/…)
    updated_at: str


@dataclass(frozen=True)
class InboxItem:
    repo: str
    number: int
    title: str
    url: str
    status: str  # ready | escalated
    reviewers: dict
    cost_usd: float


@dataclass(frozen=True)
class ProjectSpend:
    repo: str
    total_24h_usd: float
    by_provider: dict = field(default_factory=dict)


# ---- label-derived signals --------------------------------------------------
def status_from_labels(labels: list[str]) -> str:
    """Single status for a PR, by precedence (most blocking first).

    secret-missing > paused > escalated > ready > reviewing. A PR carrying a
    dispatcher tier label but none of the state labels is 'reviewing'; one with
    no dispatcher labels at all is 'unknown' (not yet classified).
    """
    s = set(labels)
    if LABEL_SECRET_MISSING in s:
        return STATUS_SECRET_MISSING
    if LABEL_PAUSED in s:
        return STATUS_PAUSED
    if LABEL_ESCALATED in s:
        return STATUS_ESCALATED
    if LABEL_READY in s:
        return STATUS_READY
    if any(l.startswith(TIER_PREFIX) for l in labels):
        return STATUS_REVIEWING
    return STATUS_UNKNOWN


def tier_from_labels(labels: list[str]) -> Optional[str]:
    for l in labels:
        if l.startswith(TIER_PREFIX):
            return l[len(TIER_PREFIX):]
    return None


def round_from_labels(labels: list[str]) -> Optional[int]:
    for l in labels:
        if l.startswith(ROUND_PREFIX):
            try:
                return int(l[len(ROUND_PREFIX):])
            except ValueError:
                return None
    return None


# ---- comment-derived signals ------------------------------------------------
def _first_json_in_body(body: str) -> Optional[dict]:
    """Parse the first fenced code block in a body as JSON (or None)."""
    if not body:
        return None
    m = _FENCED_RE.search(body)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_state(comments: list[dict], *, marker: str = STATE_MARKER) -> dict:
    """Extract the dispatcher's per-PR state JSON from its state comment.

    Display-only: the engine signs this comment, but the board does not make
    trust decisions on it (it only shows cost), so the signature is not verified
    here. Returns {} when absent or unparsable.
    """
    for c in comments:
        body = c.get("body") or ""
        if marker in body:
            data = _first_json_in_body(body)
            if data is not None:
                return data
    return {}


def cost_from_state(state: dict) -> float:
    try:
        return round(float(state.get("cumulative_cost_usd", 0.0)), 6)
    except (TypeError, ValueError):
        return 0.0


def _parse_verdict_block(block: str) -> dict:
    fields: dict = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def latest_verdicts(comments: list[dict]) -> dict:
    """Map reviewer -> their latest verdict across all verdict comments.

    'Latest' = highest round, tie-broken by emitted_at. Parses the engine's
    ``tradewatcher-verdict`` blocks; does not verify the HMAC signature (display
    only). Malformed blocks are skipped.
    """
    best: dict = {}  # reviewer -> (round, emitted_at, verdict)
    for c in comments:
        for block in _VERDICT_BLOCK_RE.findall(c.get("body") or ""):
            f = _parse_verdict_block(block)
            reviewer, verdict = f.get("reviewer"), f.get("verdict")
            if not reviewer or not verdict:
                continue
            try:
                rnd = int(f.get("round", 0))
            except ValueError:
                rnd = 0
            emitted = f.get("emitted_at", "")
            key = (rnd, emitted)
            if reviewer not in best or key > best[reviewer][:2]:
                best[reviewer] = (rnd, emitted, verdict)
    return {r: v[2] for r, v in best.items()}


# ---- spend (24h window over the global ledger issue) ------------------------
def spend_from_ledger(ledger_body: str, *, now_ts: float) -> tuple:
    """Return (total_24h_usd, by_provider) from a global-spend ledger issue body.

    Mirrors the engine's window math: sum events with ts within the trailing 24h;
    per-provider from each event's optional ``by`` map. Pure.
    """
    data = _first_json_in_body(ledger_body or "")
    events = (data or {}).get("events", [])
    if not isinstance(events, list):
        return 0.0, {}
    cutoff = now_ts - WINDOW_SECONDS
    total = 0.0
    by: dict = {}
    for ev in events:
        try:
            ts = float(ev.get("ts", 0))
            cost = float(ev.get("cost", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        if ts < cutoff:
            continue
        total += cost
        b = ev.get("by")
        if isinstance(b, dict):
            for prov, amt in b.items():
                try:
                    by[str(prov)] = round(by.get(str(prov), 0.0) + float(amt), 6)
                except (TypeError, ValueError):
                    continue
    return round(total, 6), by


# ---- assembly ---------------------------------------------------------------
def build_board_row(*, repo: str, pr: dict, labels: list[str], comments: list[dict]) -> BoardRow:
    state = parse_state(comments)
    return BoardRow(
        repo=repo,
        number=int(pr.get("number", 0)),
        title=pr.get("title", "") or "",
        url=pr.get("html_url", "") or "",
        status=status_from_labels(labels),
        tier=tier_from_labels(labels),
        round=round_from_labels(labels),
        cost_usd=cost_from_state(state),
        reviewers=latest_verdicts(comments),
        updated_at=pr.get("updated_at", "") or "",
    )


def is_actionable(status: str) -> bool:
    return status in ACTIONABLE_STATUSES


def to_inbox_item(row: BoardRow) -> InboxItem:
    return InboxItem(
        repo=row.repo, number=row.number, title=row.title, url=row.url,
        status=row.status, reviewers=row.reviewers, cost_usd=row.cost_usd,
    )


def inbox_from_rows(rows: list[BoardRow]) -> list[InboxItem]:
    """Actionable PRs (escalated first, then ready), for the approvals queue."""
    actionable = [r for r in rows if is_actionable(r.status)]
    order = {STATUS_ESCALATED: 0, STATUS_READY: 1}
    actionable.sort(key=lambda r: (order.get(r.status, 9), r.repo, r.number))
    return [to_inbox_item(r) for r in actionable]


__all__ = [
    "LABEL_PAUSED", "LABEL_ESCALATED", "LABEL_READY", "LABEL_SECRET_MISSING",
    "TIER_PREFIX", "ROUND_PREFIX", "STATE_MARKER", "GLOBAL_SPEND_MARKER",
    "USAGE_LEDGER_MARKER", "WINDOW_SECONDS",
    "STATUS_SECRET_MISSING", "STATUS_PAUSED", "STATUS_ESCALATED", "STATUS_READY",
    "STATUS_REVIEWING", "STATUS_UNKNOWN", "ACTIONABLE_STATUSES",
    "BoardRow", "InboxItem", "ProjectSpend",
    "status_from_labels", "tier_from_labels", "round_from_labels",
    "parse_state", "cost_from_state", "latest_verdicts", "spend_from_ledger",
    "build_board_row", "is_actionable", "to_inbox_item", "inbox_from_rows",
]
