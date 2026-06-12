"""Tests for scripts.dispatcher.escalation."""

from scripts.dispatcher.converge import ConvergenceState
from scripts.dispatcher.escalation import (
    EscalationTrigger,
    decide_escalation,
    should_defer_escalation,
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


def test_cost_spike_when_not_converged():
    # Over the per-PR ceiling AND still iterating (dissent) -> escalate to stop
    # burning more money. Cost takes priority over the dissent reason.
    d = decide_escalation(
        tier="routine",
        round_=1,
        round_budget=3,
        max_review_rounds=6,
        convergence=_dissenting_state(),
        changed_files=["README.md"],
        per_pr_cost_usd=10.0,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.COST_SPIKE


def test_converged_pr_over_budget_is_ready_not_escalated():
    # The reported wart: a PR that converged but whose total crossed the per-PR
    # ceiling must read as ready-for-merge, not a redundant cost escalation —
    # the spend is already incurred and no further reviews will run.
    d = decide_escalation(
        tier="high_stakes",
        round_=10,
        round_budget=2,
        max_review_rounds=10,
        convergence=_converged_state(required=("claude", "gpt", "gemini")),
        changed_files=["README.md"],
        per_pr_cost_usd=5.80,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.NONE


def test_converged_over_budget_head_lock_still_routes_to_operator_signoff():
    # Suppressing the cost escalation must NOT let a head-lock PR slip through:
    # a converged PR touching an operator-gated path still escalates for
    # sign-off (with the correct reason, not a "cost" reason).
    d = decide_escalation(
        tier="high_stakes",
        round_=10,
        round_budget=2,
        max_review_rounds=10,
        convergence=_converged_state(required=("claude", "gpt", "gemini")),
        changed_files=["backend/app/broker.py"],
        per_pr_cost_usd=5.80,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
        head_lock_paths=("backend/app/broker*.py",),
    )
    assert d.trigger == EscalationTrigger.HIGH_STAKES_AUTO


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


def test_high_stakes_touching_head_lock_path_escalates_even_if_converged():
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
        head_lock_paths=("backend/app/promotion_gate.py", "backend/app/safety_*.py"),
    )
    assert d.trigger == EscalationTrigger.HIGH_STAKES_AUTO


def test_high_stakes_head_lock_empty_by_default_no_auto_escalation():
    # With no head_lock_paths configured (generic default), a converged
    # high-stakes PR does NOT auto-escalate on path alone.
    d = decide_escalation(
        tier="high_stakes",
        round_=2,
        round_budget=2,
        max_review_rounds=6,
        convergence=_converged_state(required=("claude", "gpt", "gemini")),
        changed_files=["backend/app/promotion_gate.py"],
        per_pr_cost_usd=0.5,
        per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0,
        ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.NONE


def test_high_stakes_first_dissent_is_not_escalated():
    # New model: mid-iteration reviewer dissent is owned by the dev agent, NOT the
    # operator. A high-stakes round-1 dissent (no head-lock path) must stay silent
    # — the panel keeps iterating; the operator is pinged only on convergence, a
    # genuine stall (hard cap), or an explicit raise-hand.
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
    assert d.trigger == EscalationTrigger.NONE


def test_routine_dissent_at_budget_is_not_escalated():
    # The soft round budget no longer escalates on its own — dissent past it is
    # still the dev agent's job, up to the HARD round cap backstop.
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
    assert d.trigger == EscalationTrigger.NONE


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


def test_should_defer_escalation_only_while_iterating():
    # The cooldown exists to avoid pinging mid-fix, so it applies ONLY while the
    # PR is still iterating. A converged PR is done and waiting on the operator,
    # so its escalation must fire immediately — this is the "all approved, needs
    # your signature" ping that used to get silently dropped behind a flaky sweep.
    gated = EscalationTrigger.HIGH_STAKES_AUTO  # a cooldown-gated trigger

    # not converged + cooldown on -> defer (don't buzz mid-iteration)
    assert should_defer_escalation(trigger=gated, cooldown_minutes=10, converged=False) is True
    # CONVERGED -> never defer, even with a cooldown configured
    assert should_defer_escalation(trigger=gated, cooldown_minutes=10, converged=True) is False
    # cooldown disabled -> never defer
    assert should_defer_escalation(trigger=gated, cooldown_minutes=0, converged=False) is False
    # a non-gated (infra/budget) trigger -> never defer
    assert should_defer_escalation(
        trigger=EscalationTrigger.DAILY_COST_SPIKE, cooldown_minutes=10, converged=False
    ) is False
    # every cooldown-gated trigger defers while iterating, fires once converged
    for t in (
        EscalationTrigger.DISAGREEMENT_AFTER_BUDGET,
        EscalationTrigger.HIGH_STAKES_FIRST_DISSENT,
        EscalationTrigger.CI_PERSISTENT_FAILURE,
        EscalationTrigger.SUSPICIOUS_UNANIMOUS,
    ):
        assert should_defer_escalation(trigger=t, cooldown_minutes=10, converged=False) is True
        assert should_defer_escalation(trigger=t, cooldown_minutes=10, converged=True) is False
