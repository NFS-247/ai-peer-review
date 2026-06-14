"""PR state management via GitHub labels and a hidden state comment.

Per Section 11.5 (No long-lived state in PR branches), dispatcher state
lives in PR labels and structured comments, not in files on the branch.

Label conventions:
- ``dispatcher:tier-<tier>`` — e.g. ``dispatcher:tier-high_stakes``
- ``dispatcher:round-<n>``    — e.g. ``dispatcher:round-3``
- ``dispatcher:paused``       — set by OPERATOR PAUSE, cleared by RESUME
- ``dispatcher:escalated``    — set when an escalation has fired
- ``dispatcher:ready-for-merge`` — set when converged

Cross-run numeric state (cumulative cost, CI fix attempts, consecutive API
failures) lives in a single hidden state comment authored by the dispatcher.
The comment carries a marker and a fenced JSON block.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Optional

from .github_api import GitHubAPI


LABEL_PAUSED = "dispatcher:paused"
LABEL_ESCALATED = "dispatcher:escalated"
LABEL_READY = "dispatcher:ready-for-merge"

TIER_LABEL_PREFIX = "dispatcher:tier-"
ROUND_LABEL_PREFIX = "dispatcher:round-"


def read_tier_label(labels: list[str]) -> Optional[str]:
    for lbl in labels:
        if lbl.startswith(TIER_LABEL_PREFIX):
            return lbl[len(TIER_LABEL_PREFIX):]
    return None


def read_round_label(labels: list[str]) -> int:
    """Return the current round counter, or 0 if no label is set."""
    for lbl in labels:
        if lbl.startswith(ROUND_LABEL_PREFIX):
            try:
                return int(lbl[len(ROUND_LABEL_PREFIX):])
            except ValueError:
                continue
    return 0


def is_paused(labels: list[str]) -> bool:
    return LABEL_PAUSED in labels


def is_escalated(labels: list[str]) -> bool:
    return LABEL_ESCALATED in labels


def set_tier(api: GitHubAPI, pr_number: int, tier: str, existing_labels: list[str]) -> None:
    new_label = f"{TIER_LABEL_PREFIX}{tier}"
    # Remove any prior tier label, then add the new one.
    for lbl in existing_labels:
        if lbl.startswith(TIER_LABEL_PREFIX) and lbl != new_label:
            api.remove_label(pr_number, lbl)
    if new_label not in existing_labels:
        api.add_labels(pr_number, [new_label])


def bump_round(api: GitHubAPI, pr_number: int, existing_labels: list[str]) -> int:
    """Increment the round counter and return the new value."""
    current = read_round_label(existing_labels)
    new = current + 1
    for lbl in existing_labels:
        if lbl.startswith(ROUND_LABEL_PREFIX):
            api.remove_label(pr_number, lbl)
    api.add_labels(pr_number, [f"{ROUND_LABEL_PREFIX}{new}"])
    return new


def set_paused(api: GitHubAPI, pr_number: int, existing_labels: list[str]) -> None:
    if LABEL_PAUSED not in existing_labels:
        api.add_labels(pr_number, [LABEL_PAUSED])


def clear_paused(api: GitHubAPI, pr_number: int, existing_labels: list[str]) -> None:
    if LABEL_PAUSED in existing_labels:
        api.remove_label(pr_number, LABEL_PAUSED)


def set_escalated(api: GitHubAPI, pr_number: int, existing_labels: list[str]) -> None:
    if LABEL_ESCALATED not in existing_labels:
        api.add_labels(pr_number, [LABEL_ESCALATED])


def set_ready(api: GitHubAPI, pr_number: int, existing_labels: list[str]) -> None:
    if LABEL_READY not in existing_labels:
        api.add_labels(pr_number, [LABEL_READY])


def clear_ready(api: GitHubAPI, pr_number: int, existing_labels: list[str]) -> None:
    """Remove the ready-for-merge label.

    Called when a PR that was marked ready is being re-reviewed on new content:
    the prior convergence is stale, so the PR is NOT ready until it re-converges.
    Clearing it also un-dedupes the ready notification, so the re-convergence
    fires a FRESH 'ready for merge' ping (the notify is gated on this label).
    """
    if LABEL_READY in existing_labels:
        api.remove_label(pr_number, LABEL_READY)


# ---- Cross-run numeric state via a hidden state comment --------------------

STATE_COMMENT_MARKER = "<!-- tradewatcher-dispatcher-state -->"
_STATE_JSON_RE = re.compile(
    r"```tradewatcher-dispatcher-state\s*\n(.*?)\n```",
    re.DOTALL,
)


@dataclass
class CrossRunState:
    """Numeric state that must persist across workflow runs for one PR."""

    cumulative_cost_usd: float = 0.0
    ci_fix_attempts: int = 0
    consecutive_api_failures: int = 0
    # Deferred (cooldown-gated) escalation: when reviewers request changes (or
    # CI persistently fails), the ping is held until the dev agent goes quiet
    # for the cooldown window. These track the pending stall; pending_since == 0
    # means none pending. ``escalated_head_sha`` records the head a cooldown
    # escalation last fired at, so a new commit can supersede it.
    pending_escalation_since: float = 0.0
    pending_escalation_head_sha: str = ""
    pending_escalation_trigger: str = ""
    pending_escalation_reason_short: str = ""
    pending_escalation_detail: str = ""
    escalated_head_sha: str = ""
    state_comment_id: Optional[int] = None  # not serialized into the block

    def has_pending_escalation(self) -> bool:
        return bool(self.pending_escalation_since)

    def clear_pending_escalation(self) -> None:
        self.pending_escalation_since = 0.0
        self.pending_escalation_head_sha = ""
        self.pending_escalation_trigger = ""
        self.pending_escalation_reason_short = ""
        self.pending_escalation_detail = ""

    def _payload(self) -> dict:
        payload = {
            "cumulative_cost_usd": round(self.cumulative_cost_usd, 6),
            "ci_fix_attempts": self.ci_fix_attempts,
            "consecutive_api_failures": self.consecutive_api_failures,
        }
        # Cut-1 fields are added ONLY when non-default, so a state with no
        # pending escalation serializes (and signs) byte-for-byte like the
        # pre-Cut-1 format — existing signed state comments still verify after
        # the tag bump.
        if self.pending_escalation_since:
            payload["pending_escalation_since"] = round(self.pending_escalation_since, 3)
            payload["pending_escalation_head_sha"] = self.pending_escalation_head_sha
            payload["pending_escalation_trigger"] = self.pending_escalation_trigger
            payload["pending_escalation_reason_short"] = self.pending_escalation_reason_short
            payload["pending_escalation_detail"] = self.pending_escalation_detail
        if self.escalated_head_sha:
            payload["escalated_head_sha"] = self.escalated_head_sha
        return payload

    def signing_string(self) -> str:
        return json.dumps(self._payload(), sort_keys=True, separators=(",", ":"))

    def signature(self, secret: str) -> str:
        return hmac.new(
            secret.encode("utf-8"),
            self.signing_string().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def to_block(self, secret: str) -> str:
        payload = self._payload()
        payload["signature"] = self.signature(secret)
        return (
            f"{STATE_COMMENT_MARKER}\n\n"
            "_Dispatcher internal state. Do not edit._\n\n"
            "```tradewatcher-dispatcher-state\n"
            f"{json.dumps(payload, indent=2)}\n"
            "```\n"
        )


def read_cross_run_state(
    api: GitHubAPI,
    pr_number: int,
    dispatcher_bot_login: str,
    secret: str,
) -> CrossRunState:
    """Read the dispatcher state comment, if any.

    A state comment is trusted only if it is authored by the dispatcher bot
    AND carries a valid HMAC signature keyed by ``secret``. A forged human or
    other-workflow state comment is ignored. The latest valid state wins.
    """
    latest: Optional[CrossRunState] = None
    for c in api.list_pr_comments(pr_number):  # chronological (oldest first)
        if c.author_login != dispatcher_bot_login:
            continue
        if STATE_COMMENT_MARKER not in c.body:
            continue
        match = _STATE_JSON_RE.search(c.body)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        candidate = CrossRunState(
            cumulative_cost_usd=float(data.get("cumulative_cost_usd", 0.0)),
            ci_fix_attempts=int(data.get("ci_fix_attempts", 0)),
            consecutive_api_failures=int(data.get("consecutive_api_failures", 0)),
            pending_escalation_since=float(data.get("pending_escalation_since", 0.0)),
            pending_escalation_head_sha=str(data.get("pending_escalation_head_sha", "")),
            pending_escalation_trigger=str(data.get("pending_escalation_trigger", "")),
            pending_escalation_reason_short=str(data.get("pending_escalation_reason_short", "")),
            pending_escalation_detail=str(data.get("pending_escalation_detail", "")),
            escalated_head_sha=str(data.get("escalated_head_sha", "")),
            state_comment_id=c.id,
        )
        provided_sig = data.get("signature")
        if not provided_sig or not hmac.compare_digest(
            candidate.signature(secret), str(provided_sig)
        ):
            continue
        latest = candidate
    return latest if latest is not None else CrossRunState()


def write_cross_run_state(
    api: GitHubAPI,
    pr_number: int,
    state: CrossRunState,
    secret: str,
) -> None:
    """Persist the cross-run state as a signed, append-only comment.

    Readers take the latest VALID (signed) dispatcher-authored state comment,
    so the newest wins. Append-only avoids needing comment-edit permissions.
    """
    api.post_comment(pr_number, state.to_block(secret))


__all__ = [
    "LABEL_PAUSED",
    "LABEL_ESCALATED",
    "LABEL_READY",
    "TIER_LABEL_PREFIX",
    "ROUND_LABEL_PREFIX",
    "STATE_COMMENT_MARKER",
    "CrossRunState",
    "read_tier_label",
    "read_round_label",
    "is_paused",
    "is_escalated",
    "set_tier",
    "bump_round",
    "set_paused",
    "clear_paused",
    "set_escalated",
    "set_ready",
    "clear_ready",
    "read_cross_run_state",
    "write_cross_run_state",
]
