"""Convergence checker for the AI peer review dispatcher.

Implements Section 12.5 of the design doc. A PR is "converged and ready for
operator merge" if and only if all of these are true:

1. Every required reviewer for this PR's tier has emitted at least one
   verdict record.
2. The most recent verdict from each required reviewer is "approve".
3. Every counted verdict has head_sha matching the PR's current head SHA.
4. Every counted verdict has diff_sha256 matching the dispatcher-computed
   SHA-256 of the current diff.
5. CI on the current head SHA is "success".
6. No OPERATOR PAUSE is currently active on the PR.

Freeform text does NOT count. GitHub's native "Approve" does NOT count. Only
dispatcher-emitted verdict records do.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .verdict import VERDICT_APPROVE, Verdict


class CIStatus(str, Enum):
    SUCCESS = "success"
    PENDING = "pending"
    FAILURE = "failure"
    NONE = "none"


@dataclass(frozen=True)
class ConvergenceState:
    converged: bool
    reason: str
    required_reviewers: tuple[str, ...]
    received_from: tuple[str, ...]
    missing_from: tuple[str, ...]
    stale_from: tuple[str, ...]
    non_approving_from: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "converged": self.converged,
            "reason": self.reason,
            "required_reviewers": list(self.required_reviewers),
            "received_from": list(self.received_from),
            "missing_from": list(self.missing_from),
            "stale_from": list(self.stale_from),
            "non_approving_from": list(self.non_approving_from),
        }


def _latest_per_reviewer(
    verdicts: Iterable[Verdict],
) -> dict[str, Verdict]:
    latest: dict[str, Verdict] = {}
    for v in verdicts:
        prev = latest.get(v.reviewer)
        if prev is None or v.emitted_at > prev.emitted_at:
            latest[v.reviewer] = v
    return latest


def check_convergence(
    *,
    verdicts: Iterable[Verdict],
    required_reviewers: Iterable[str],
    current_head_sha: str,
    current_diff_sha256: str,
    ci_status: CIStatus,
    operator_pause_active: bool,
) -> ConvergenceState:
    required = tuple(required_reviewers)

    if operator_pause_active:
        return ConvergenceState(
            converged=False,
            reason="operator_pause_active",
            required_reviewers=required,
            received_from=(),
            missing_from=required,
            stale_from=(),
            non_approving_from=(),
        )

    if ci_status != CIStatus.SUCCESS:
        return ConvergenceState(
            converged=False,
            reason=f"ci_not_success:{ci_status.value}",
            required_reviewers=required,
            received_from=(),
            missing_from=required,
            stale_from=(),
            non_approving_from=(),
        )

    latest = _latest_per_reviewer(verdicts)

    missing = tuple(r for r in required if r not in latest)
    if missing:
        return ConvergenceState(
            converged=False,
            reason="missing_verdicts",
            required_reviewers=required,
            received_from=tuple(latest.keys()),
            missing_from=missing,
            stale_from=(),
            non_approving_from=(),
        )

    stale = tuple(
        r for r in required
        if latest[r].head_sha != current_head_sha
        or latest[r].diff_sha256 != current_diff_sha256
    )
    if stale:
        return ConvergenceState(
            converged=False,
            reason="stale_verdicts",
            required_reviewers=required,
            received_from=tuple(latest.keys()),
            missing_from=(),
            stale_from=stale,
            non_approving_from=(),
        )

    non_approving = tuple(
        r for r in required if latest[r].verdict != VERDICT_APPROVE
    )
    if non_approving:
        return ConvergenceState(
            converged=False,
            reason="non_approving_verdicts",
            required_reviewers=required,
            received_from=tuple(latest.keys()),
            missing_from=(),
            stale_from=(),
            non_approving_from=non_approving,
        )

    return ConvergenceState(
        converged=True,
        reason="all_required_reviewers_approve",
        required_reviewers=required,
        received_from=required,
        missing_from=(),
        stale_from=(),
        non_approving_from=(),
    )


__all__ = ["CIStatus", "ConvergenceState", "check_convergence"]
