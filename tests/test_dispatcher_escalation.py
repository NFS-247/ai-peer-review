"""Tests for scripts.dispatcher.escalation."""

from scripts.dispatcher.converge import ConvergenceState
from scripts.dispatcher.escalation import (
    EscalationTrigger,
    decide_escalation,
)


def _converged_state(required=("claude", "gpt")) -> ConvergenceState:
    return ConvergenceState(
        converged=True,
        reason="all_required_reviewers_approve",
        required_reviewers=required,
        received_from=required,
        missing_from=(),
        stale_from=(),
        non_approving_from=(),
    )


def _dissenting_state(required=("claude", "gpt"), dissenters=("gpt",)) -> ConvergenceState:
    return ConvergenceState(
        converged=False,
        reason="non_approving_verdicts",
        required_reviewers=required,
        received_from=required,
        missing_from=(),
        stale_from=(),
        non_approving_from=dissenters,
    )


def test_no_escalation_when_converged_routine():
    d = decide_escalation(
        tier="routine",
        round_=1,
        round_budget=3,
        max_review_rounds=6,
        convergence=_converged_state(),
        changed_files=["README.md"],
        per_pr_cost_usd=0.10,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.NONE


def test_cost_spike_triggers_first():
    d = decide_escalation(
        tier="routine",
        round_=1,
        round_budget=3,
        max_review_rounds=6,
        convergence=_converged_state(),
        changed_files=["README.md"],
        per_pr_cost_usd=10.0,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.COST_SPIKE


def test_api_outage_triggers():
    d = decide_escalation(
        tier="routine",
        round_=1,
        round_budget=3,
        max_review_rounds=6,
        convergence=_converged_state(),
        changed_files=["README.md"],
        per_pr_cost_usd=0.0,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=45,
        ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.API_OUTAGE


def test_hard_round_cap():
    d = decide_escalation(
        tier="routine",
        round_=6,
        round_budget=3,
        max_review_rounds=6,
        convergence=_dissenting_state(),
        changed_files=["README.md"],
        per_pr_cost_usd=0.0,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.HARD_ROUND_CAP


def test_high_stakes_touching_promotion_gate_escalates_even_if_converged():
    d = decide_escalation(
        tier="high_stakes",
        round_=1,
        round_budget=2,
        max_review_rounds=6,
        convergence=_converged_state(required=("claude", "gpt", "gemini")),
        changed_files=["backend/app/promotion_gate.py"],
        per_pr_cost_usd=0.5,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.HIGH_STAKES_AUTO


def test_high_stakes_first_dissent_escalates_immediately():
    d = decide_escalation(
        tier="high_stakes",
        round_=1,
        round_budget=2,
        max_review_rounds=6,
        convergence=_dissenting_state(
            required=("claude", "gpt", "gemini"),
            dissenters=("gemini",),
        ),
        changed_files=["backend/app/some_module.py"],  # not a head-lock path
        per_pr_cost_usd=0.0,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.HIGH_STAKES_FIRST_DISSENT


def test_routine_dissent_at_budget_escalates():
    d = decide_escalation(
        tier="routine",
        round_=3,
        round_budget=3,
        max_review_rounds=6,
        convergence=_dissenting_state(),
        changed_files=["README.md"],
        per_pr_cost_usd=0.0,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.DISAGREEMENT_AFTER_BUDGET


def test_suspicious_unanimous_high_stakes_round_one():
    d = decide_escalation(
        tier="high_stakes",
        round_=1,
        round_budget=2,
        max_review_rounds=6,
        convergence=_converged_state(required=("claude", "gpt", "gemini")),
        changed_files=["backend/app/some_innocuous.py"],
        per_pr_cost_usd=0.5,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.SUSPICIOUS_UNANIMOUS


def test_ci_persistent_failure():
    d = decide_escalation(
        tier="backend",
        round_=2,
        round_budget=3,
        max_review_rounds=6,
        convergence=_converged_state(),
        changed_files=["backend/app/something.py"],
        per_pr_cost_usd=0.0,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=3,
    )
    assert d.trigger == EscalationTrigger.CI_PERSISTENT_FAILURE


# ---- new triggers from GPT review #2 and #3 on PR #74 ----------------------

def test_daily_cost_spike_takes_priority():
    d = decide_escalation(
        tier="routine",
        round_=1,
        round_budget=3,
        max_review_rounds=6,
        convergence=_converged_state(),
        changed_files=["README.md"],
        per_pr_cost_usd=0.5,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
        daily_cost_usd=25.0,
        daily_cost_ceiling_usd=20.0,
    )
    assert d.trigger == EscalationTrigger.DAILY_COST_SPIKE


def test_daily_cost_under_ceiling_no_escalation():
    d = decide_escalation(
        tier="routine",
        round_=1,
        round_budget=3,
        max_review_rounds=6,
        convergence=_converged_state(),
        changed_files=["README.md"],
        per_pr_cost_usd=0.5,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
        daily_cost_usd=10.0,
        daily_cost_ceiling_usd=20.0,
    )
    assert d.trigger == EscalationTrigger.NONE


def test_required_reviewer_unavailable_escalates_immediately():
    # GPT review #3: first required-reviewer failure must not leave a stuck PR.
    d = decide_escalation(
        tier="backend",
        round_=1,
        round_budget=3,
        max_review_rounds=6,
        convergence=_dissenting_state(dissenters=()),
        changed_files=["backend/app/x.py"],
        per_pr_cost_usd=0.1,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
        required_reviewer_unavailable=True,
    )
    assert d.trigger == EscalationTrigger.REQUIRED_REVIEWER_UNAVAILABLE
