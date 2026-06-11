"""Escalation decision logic.

Implements Section 5 of the design doc. Decides whether a PR's current state
requires escalation to the operator. Returns a structured reason that the
email builder can render.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .classify import _match_any
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


# Triggers that mean "the operator should look at this PR's review outcome".
# These are cooldown-gated: the dispatcher waits until the dev agent has gone
# quiet (no new commit for the cooldown window) before pinging, so the phone
# never fires mid-iteration while fixes are still landing. Everything NOT in
# this set is a stop-the-world infra/budget condition and pings immediately.
COOLDOWN_GATED_TRIGGERS = frozenset({
    EscalationTrigger.DISAGREEMENT_AFTER_BUDGET,
    EscalationTrigger.HIGH_STAKES_FIRST_DISSENT,
    EscalationTrigger.CI_PERSISTENT_FAILURE,
    EscalationTrigger.HIGH_STAKES_AUTO,
    EscalationTrigger.SUSPICIOUS_UNANIMOUS,
})


# "The reviewers ran but couldn't converge" — the PR WAS reviewed and the panel is
# split or stuck, so the operator must break the tie. These route to the
# disagreement Chat card (which shows the split + Approve-to-override / Open PR),
# distinct from a budget stop (DAILY_COST_SPIKE) where reviews may not have run.
# COST_SPIKE belongs here, not with budget: it only fires when not-converged AND
# reviews have already run (the spend IS the reviews) — see decide_escalation.
DISAGREEMENT_TRIGGERS = frozenset({
    EscalationTrigger.HARD_ROUND_CAP,
    EscalationTrigger.DISAGREEMENT_AFTER_BUDGET,
    EscalationTrigger.HIGH_STAKES_FIRST_DISSENT,
    EscalationTrigger.COST_SPIKE,
})


# Head-lock paths are PROJECT-SPECIFIC (e.g. a trading system's broker/safety/
# promotion modules that must get explicit operator sign-off before merge even
# when the AI reviewers converge). They live in the repo's .peer-review.json
# `head_lock_paths`; the generic default is empty, so a repo with no config
# relies on tier classification alone. Matched with the same fnmatch-glob
# semantics as classify's path patterns (so ``.github/workflows/**`` etc. work).
HEAD_LOCK_PATH_PATTERNS: tuple[str, ...] = ()


def _diff_touches_head_lock(
    changed_files: list[str],
    head_lock_paths: Sequence[str] = HEAD_LOCK_PATH_PATTERNS,
) -> bool:
    """True if any changed file matches a head-lock glob (operator-gated path)."""
    for path in changed_files:
        if _match_any(path, head_lock_paths):
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
    head_lock_paths: Sequence[str] = (),
) -> EscalationDecision:
    """Decide whether this PR should be escalated. Order of checks matters."""

    # The 24h DAILY ceiling is a project-wide stop: it pauses OTHER in-flight
    # PRs and the operator must see it, so it escalates even if this PR has
    # converged.
    if daily_cost_ceiling_usd > 0 and daily_cost_usd >= daily_cost_ceiling_usd:
        return EscalationDecision(
            trigger=EscalationTrigger.DAILY_COST_SPIKE,
            reason_short="24-hour dispatcher spend ceiling reached",
            detail=f"Dispatcher spend across all PRs in the last 24h is "
                   f"${daily_cost_usd:.2f} (ceiling ${daily_cost_ceiling_usd:.2f}). "
                   f"All in-flight reviews are paused pending operator review.",
        )

    # A per-PR cost ceiling exists to stop BURNING MORE money iterating on a PR
    # that won't converge. Once the PR HAS converged, the spend is already
    # incurred and no further reviews will run, so a per-PR cost escalation is
    # pure noise — the PR is simply ready for merge. Suppress it when converged
    # and fall through; a converged head-lock PR then correctly routes to the
    # operator-sign-off path below instead of showing a misleading "cost" reason.
    if per_pr_cost_usd >= per_pr_cost_ceiling_usd and not convergence.converged:
        return EscalationDecision(
            trigger=EscalationTrigger.COST_SPIKE,
            reason_short="per-PR cost ceiling reached",
            detail=f"This PR has consumed ${per_pr_cost_usd:.2f} in API tokens "
                   f"(ceiling ${per_pr_cost_ceiling_usd:.2f}) without converging.",
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

    # "Out of rounds WITHOUT converging." A PR that converged on its final
    # allowed round is ready, not capped — suppress when converged (same
    # principle as the per-PR cost suppression above).
    if round_ >= max_review_rounds and not convergence.converged:
        return EscalationDecision(
            trigger=EscalationTrigger.HARD_ROUND_CAP,
            reason_short="hard round cap reached",
            detail=f"PR has reached {max_review_rounds} review rounds without convergence.",
        )

    if tier == "high_stakes" and _diff_touches_head_lock(changed_files, head_lock_paths):
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


def cooldown_elapsed(
    *,
    pending_since: float,
    pending_head_sha: str,
    current_head_sha: str,
    now_ts: float,
    cooldown_minutes: int,
) -> bool:
    """Whether a deferred escalation is now due to ping. Pure function.

    Due iff there IS a pending escalation, the cooldown is enabled, the head
    has NOT changed since it was recorded (a new commit supersedes/re-arms),
    and the quiet window has elapsed.
    """
    if not pending_since or cooldown_minutes <= 0:
        return False
    if pending_head_sha != current_head_sha:
        return False  # superseded by a new commit
    return (now_ts - pending_since) >= cooldown_minutes * 60


__all__ = [
    "EscalationTrigger",
    "EscalationDecision",
    "decide_escalation",
    "HEAD_LOCK_PATH_PATTERNS",
    "COOLDOWN_GATED_TRIGGERS",
    "DISAGREEMENT_TRIGGERS",
    "cooldown_elapsed",
]
