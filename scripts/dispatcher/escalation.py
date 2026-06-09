"""Escalation decision logic.

Implements Section 5 of the design doc. Decides whether a PR's current state
requires escalation to the operator. Returns a structured reason that the
email builder can render.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .converge import ConvergenceState


class EscalationTrigger(str, Enum):
    NONE = "none"
    DISAGREEMENT_AFTER_BUDGET = "disagreement_after_budget"
    HIGH_STAKES_FIRST_DISSENT = "high_stakes_first_dissent"
    HARD_ROUND_CAP = "hard_round_cap"
    SUSPICIOUS_UNANIMOUS = "suspicious_unanimous"
    COST_SPIKE = "cost_spike"
    DAILY_COST_SPIKE = "daily_cost_spike"
    REQUIRED_REVIEWER_UNAVAILABLE = "required_reviewer_unavailable"
    API_OUTAGE = "api_outage"
    CI_PERSISTENT_FAILURE = "ci_persistent_failure"
    HIGH_STAKES_AUTO = "high_stakes_auto"
    HEAD_LOCK_FILES = "head_lock_files"


@dataclass(frozen=True)
class EscalationDecision:
    trigger: EscalationTrigger
    reason_short: str
    detail: str


HEAD_LOCK_PATH_PATTERNS = (
    "backend/app/promotion_gate.py",
    "backend/app/safety_*.py",
    "backend/app/broker*.py",
    "backend/app/schwab*.py",
    ".github/workflows/",
    "docs/tradewatcher_operating_contract_",
)


def _diff_touches_head_lock(changed_files: list[str]) -> bool:
    for path in changed_files:
        for pat in HEAD_LOCK_PATH_PATTERNS:
            if pat.endswith("*.py"):
                root = pat[:-4]
                if path.startswith(root) and path.endswith(".py"):
                    return True
            elif pat.endswith("/"):
                if path.startswith(pat):
                    return True
            elif pat.endswith("_"):
                if path.startswith(pat):
                    return True
            else:
                if path == pat:
                    return True
    return False


def decide_escalation(
    *,
    tier: str,
    round_: int,
    round_budget: int,
    max_review_rounds: int,
    convergence: ConvergenceState,
    changed_files: list[str],
    per_pr_cost_usd: float,
    per_pr_cost_ceiling_usd: float,
    api_outage_minutes: int,
    ci_failure_count_after_fix_attempts: int,
    daily_cost_usd: float = 0.0,
    daily_cost_ceiling_usd: float = 0.0,
    required_reviewer_unavailable: bool = False,
) -> EscalationDecision:
    """Decide whether this PR should be escalated. Order of checks matters."""

    if daily_cost_ceiling_usd > 0 and daily_cost_usd >= daily_cost_ceiling_usd:
        return EscalationDecision(
            trigger=EscalationTrigger.DAILY_COST_SPIKE,
            reason_short="24-hour dispatcher spend ceiling reached",
            detail=f"Dispatcher spend across all PRs in the last 24h is "
                   f"${daily_cost_usd:.2f} (ceiling ${daily_cost_ceiling_usd:.2f}). "
                   f"All in-flight reviews are paused pending operator review.",
        )

    if per_pr_cost_usd >= per_pr_cost_ceiling_usd:
        return EscalationDecision(
            trigger=EscalationTrigger.COST_SPIKE,
            reason_short="per-PR cost ceiling reached",
            detail=f"This PR has consumed ${per_pr_cost_usd:.2f} in API tokens "
                   f"(ceiling ${per_pr_cost_ceiling_usd:.2f}).",
        )

    # A required reviewer that cannot be called at all (missing key or provider
    # outage this round) is escalated immediately rather than waiting for a
    # second event that may never come. This prevents a stuck PR after the
    # first failure.
    if required_reviewer_unavailable:
        return EscalationDecision(
            trigger=EscalationTrigger.REQUIRED_REVIEWER_UNAVAILABLE,
            reason_short="a required reviewer could not be reached",
            detail="A required reviewer for this tier could not be called "
                   "this round (missing credentials or provider error). The "
                   "PR cannot converge without it; escalating so it does not "
                   "sit stuck.",
        )

    if api_outage_minutes >= 30:
        return EscalationDecision(
            trigger=EscalationTrigger.API_OUTAGE,
            reason_short="an AI API has been down >30 minutes",
            detail=f"Reviewer API outage lasting {api_outage_minutes} minutes.",
        )

    if ci_failure_count_after_fix_attempts >= 2:
        return EscalationDecision(
            trigger=EscalationTrigger.CI_PERSISTENT_FAILURE,
            reason_short="CI is persistently failing",
            detail="CI has failed across two review rounds without being "
                   "fixed. The change needs a fix pushed before it can merge.",
        )

    if round_ >= max_review_rounds:
        return EscalationDecision(
            trigger=EscalationTrigger.HARD_ROUND_CAP,
            reason_short="hard round cap reached",
            detail=f"PR has reached {max_review_rounds} review rounds without convergence.",
        )

    if tier == "high_stakes" and _diff_touches_head_lock(changed_files):
        return EscalationDecision(
            trigger=EscalationTrigger.HIGH_STAKES_AUTO,
            reason_short="high-stakes file changed; operator review required",
            detail="This PR touches files that require explicit operator approval "
                   "before merge, regardless of AI convergence.",
        )

    if convergence.non_approving_from:
        if tier == "high_stakes" and round_ == 1:
            return EscalationDecision(
                trigger=EscalationTrigger.HIGH_STAKES_FIRST_DISSENT,
                reason_short="high-stakes dissent on first review",
                detail=f"Reviewers requested changes: "
                       f"{', '.join(convergence.non_approving_from)}",
            )
        if round_ >= round_budget:
            return EscalationDecision(
                trigger=EscalationTrigger.DISAGREEMENT_AFTER_BUDGET,
                reason_short="reviewers disagreed past round budget",
                detail=f"After {round_} rounds, reviewers still requesting "
                       f"changes: {', '.join(convergence.non_approving_from)}",
            )

    if (
        tier == "high_stakes"
        and round_ == 1
        and convergence.converged
        and not convergence.non_approving_from
    ):
        return EscalationDecision(
            trigger=EscalationTrigger.SUSPICIOUS_UNANIMOUS,
            reason_short="fast unanimous approval on high-stakes PR",
            detail="All three reviewers approved within the first round with no "
                   "concerns. Sometimes a sign of rubber-stamping. Operator confirm.",
        )

    return EscalationDecision(
        trigger=EscalationTrigger.NONE,
        reason_short="no escalation",
        detail="",
    )


__all__ = [
    "EscalationTrigger",
    "EscalationDecision",
    "decide_escalation",
    "HEAD_LOCK_PATH_PATTERNS",
]
