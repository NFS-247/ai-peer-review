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


def test_high_stakes_first_dissent_defers_as_stall():
    # Mid-iteration reviewer dissent is owned by the dev agent: a high-stakes
    # round-1 dissent (no head-lock path) yields a DEFERRED stall escalation —
    # cooldown-gated, so it does NOT ping while the panel iterates; it fires only
    # once the change goes quiet past the cooldown (see the orchestrator tests).
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
    assert d.trigger == EscalationTrigger.DISAGREEMENT_STALLED
    assert should_defer_escalation(           # deferred -> no ping while iterating
        trigger=d.trigger, cooldown_minutes=10, converged=False
    ) is True


def test_routine_dissent_defers_as_stall():
    # The soft round budget never escalates on its own; a dissent past it now
    # yields a DEFERRED stall escalation (fires only once quiet) — not an immediate
    # ping, and not permanent silence.
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
    assert d.trigger == EscalationTrigger.DISAGREEMENT_STALLED
    assert should_defer_escalation(
        trigger=d.trigger, cooldown_minutes=10, converged=False
    ) is True


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


def test_head_lock_dissent_prefers_high_stakes_auto_over_stall():
    # A head-lock PR with a dissent escalates as HIGH_STAKES_AUTO (operator sign-off
    # required) — checked BEFORE the stalled-disagreement path, so the more specific
    # trigger wins.
    d = decide_escalation(
        tier="high_stakes", round_=1, round_budget=2, max_review_rounds=6,
        convergence=_dissenting_state(required=("claude", "gpt", "gemini"),
                                      dissenters=("gpt",)),
        changed_files=["backend/app/broker.py"],
        per_pr_cost_usd=0.0, per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0, ci_failure_count_after_fix_attempts=0,
        head_lock_paths=("backend/app/broker*.py",),
    )
    assert d.trigger == EscalationTrigger.HIGH_STAKES_AUTO


def test_round_cap_prefers_hard_round_cap_over_stall():
    # At the hard round cap, a non-converged split is HARD_ROUND_CAP (checked first).
    d = decide_escalation(
        tier="routine", round_=6, round_budget=3, max_review_rounds=6,
        convergence=_dissenting_state(), changed_files=["README.md"],
        per_pr_cost_usd=0.0, per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0, ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.HARD_ROUND_CAP


def test_stalled_disagreement_is_a_deferred_disagreement_trigger():
    # DISAGREEMENT_STALLED routes to the disagreement Chat card (split + Approve-
    # override / Send-back / Block) and is cooldown-gated (deferred until quiet).
    from scripts.dispatcher.escalation import (
        DISAGREEMENT_TRIGGERS, COOLDOWN_GATED_TRIGGERS,
    )
    assert EscalationTrigger.DISAGREEMENT_STALLED in DISAGREEMENT_TRIGGERS
    assert EscalationTrigger.DISAGREEMENT_STALLED in COOLDOWN_GATED_TRIGGERS


def test_missing_reviewer_is_not_a_stall():
    # A reviewer that never reported (non_approving_from empty) is NOT a stall — it's
    # transient or already REQUIRED_REVIEWER_UNAVAILABLE. Only a real dissent
    # (someone requested changes) triggers DISAGREEMENT_STALLED.
    state = ConvergenceState(
        converged=False, reason="missing_verdicts",
        required_reviewers=("claude", "gpt"), received_from=("claude",),
        missing_from=("gpt",), stale_from=(), non_approving_from=(),
    )
    d = decide_escalation(
        tier="routine", round_=2, round_budget=3, max_review_rounds=6,
        convergence=state, changed_files=["README.md"],
        per_pr_cost_usd=0.0, per_pr_cost_ceiling_usd=5.0,
        api_outage_minutes=0, ci_failure_count_after_fix_attempts=0,
    )
    assert d.trigger == EscalationTrigger.NONE
