"""Tests for scripts.dispatcher.converge."""

from datetime import datetime, timedelta, timezone

from scripts.dispatcher.converge import (
    CIStatus,
    check_convergence,
)
from scripts.dispatcher.verdict import (
    VERDICT_APPROVE,
    VERDICT_REQUEST_CHANGES,
    build_verdict,
)


HEAD_SHA = "deadbeefcafebabe"
OTHER_SHA = "0123456789abcdef"
DIFF_SHA = "a" * 64
OTHER_DIFF_SHA = "b" * 64


def _v(reviewer, verdict, head_sha=HEAD_SHA, diff_sha=DIFF_SHA, minutes_ago=0):
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return build_verdict(
        reviewer=reviewer,
        verdict=verdict,
        pr_number=1,
        head_sha=head_sha,
        diff_sha256=diff_sha,
        tier="routine",
        round_=1,
        emitted_at=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def test_converged_when_all_required_approve():
    s = check_convergence(
        verdicts=[_v("claude", VERDICT_APPROVE), _v("gpt", VERDICT_APPROVE)],
        required_reviewers=["claude", "gpt"],
        current_head_sha=HEAD_SHA,
        current_diff_sha256=DIFF_SHA,
        ci_status=CIStatus.SUCCESS,
        operator_pause_active=False,
    )
    assert s.converged
    assert s.reason == "all_required_reviewers_approve"


def test_not_converged_when_one_missing():
    s = check_convergence(
        verdicts=[_v("claude", VERDICT_APPROVE)],
        required_reviewers=["claude", "gpt"],
        current_head_sha=HEAD_SHA,
        current_diff_sha256=DIFF_SHA,
        ci_status=CIStatus.SUCCESS,
        operator_pause_active=False,
    )
    assert not s.converged
    assert s.reason == "missing_verdicts"
    assert "gpt" in s.missing_from


def test_not_converged_when_ci_pending():
    s = check_convergence(
        verdicts=[_v("claude", VERDICT_APPROVE), _v("gpt", VERDICT_APPROVE)],
        required_reviewers=["claude", "gpt"],
        current_head_sha=HEAD_SHA,
        current_diff_sha256=DIFF_SHA,
        ci_status=CIStatus.PENDING,
        operator_pause_active=False,
    )
    assert not s.converged
    assert "ci_not_success" in s.reason


def test_not_converged_when_paused():
    s = check_convergence(
        verdicts=[_v("claude", VERDICT_APPROVE), _v("gpt", VERDICT_APPROVE)],
        required_reviewers=["claude", "gpt"],
        current_head_sha=HEAD_SHA,
        current_diff_sha256=DIFF_SHA,
        ci_status=CIStatus.SUCCESS,
        operator_pause_active=True,
    )
    assert not s.converged
    assert s.reason == "operator_pause_active"


def test_not_converged_when_verdict_is_for_old_head():
    s = check_convergence(
        verdicts=[
            _v("claude", VERDICT_APPROVE, head_sha=OTHER_SHA),
            _v("gpt", VERDICT_APPROVE),
        ],
        required_reviewers=["claude", "gpt"],
        current_head_sha=HEAD_SHA,
        current_diff_sha256=DIFF_SHA,
        ci_status=CIStatus.SUCCESS,
        operator_pause_active=False,
    )
    assert not s.converged
    assert s.reason == "stale_verdicts"
    assert "claude" in s.stale_from


def test_not_converged_when_diff_sha_mismatches():
    s = check_convergence(
        verdicts=[
            _v("claude", VERDICT_APPROVE, diff_sha=OTHER_DIFF_SHA),
            _v("gpt", VERDICT_APPROVE),
        ],
        required_reviewers=["claude", "gpt"],
        current_head_sha=HEAD_SHA,
        current_diff_sha256=DIFF_SHA,
        ci_status=CIStatus.SUCCESS,
        operator_pause_active=False,
    )
    assert not s.converged
    assert s.reason == "stale_verdicts"


def test_not_converged_when_one_dissents():
    s = check_convergence(
        verdicts=[
            _v("claude", VERDICT_APPROVE),
            _v("gpt", VERDICT_REQUEST_CHANGES),
        ],
        required_reviewers=["claude", "gpt"],
        current_head_sha=HEAD_SHA,
        current_diff_sha256=DIFF_SHA,
        ci_status=CIStatus.SUCCESS,
        operator_pause_active=False,
    )
    assert not s.converged
    assert s.reason == "non_approving_verdicts"
    assert "gpt" in s.non_approving_from


def test_latest_verdict_wins_over_earlier_one():
    # Earlier verdict requested changes; later one approves.
    s = check_convergence(
        verdicts=[
            _v("claude", VERDICT_REQUEST_CHANGES, minutes_ago=10),
            _v("claude", VERDICT_APPROVE, minutes_ago=0),
            _v("gpt", VERDICT_APPROVE),
        ],
        required_reviewers=["claude", "gpt"],
        current_head_sha=HEAD_SHA,
        current_diff_sha256=DIFF_SHA,
        ci_status=CIStatus.SUCCESS,
        operator_pause_active=False,
    )
    assert s.converged


def test_high_stakes_requires_three_reviewers():
    s = check_convergence(
        verdicts=[_v("claude", VERDICT_APPROVE), _v("gpt", VERDICT_APPROVE)],
        required_reviewers=["claude", "gpt", "gemini"],
        current_head_sha=HEAD_SHA,
        current_diff_sha256=DIFF_SHA,
        ci_status=CIStatus.SUCCESS,
        operator_pause_active=False,
    )
    assert not s.converged
    assert "gemini" in s.missing_from
