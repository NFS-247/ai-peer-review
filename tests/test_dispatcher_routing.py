"""Tests for event routing, self-loop guard, and trusted-verdict source.

These cover GPT review findings #2 (check_run handling), #3 (self-loop),
and #4 (forged verdict rejection) on PR #74.

The functions under test interact with the GitHub API, so we use a tiny
fake API object that records calls and returns canned data.
"""

from dataclasses import dataclass

import scripts.dispatcher.main as dispatcher_main
from scripts.dispatcher.github_api import PRComment
from scripts.dispatcher.main import (
    DISPATCHER_BOT_LOGIN,
    OWN_WORKFLOW_CHECK_NAMES,
    _pr_number_from_event,
    _trusted_existing_verdicts,
)
from scripts.dispatcher.verdict import (
    VERDICT_APPROVE,
    build_verdict,
    compute_diff_sha256,
    compute_verdict_signature,
)


SECRET = "test-dispatcher-secret"
WRONG_SECRET = "not-the-real-secret"


# ---- #2: check_run event resolves PR number --------------------------------

def test_pr_number_from_check_run_event():
    event = {
        "check_run": {
            "name": "pytest",
            "pull_requests": [{"number": 99}],
        }
    }
    assert _pr_number_from_event(event, "check_run") == 99


def test_pr_number_from_check_run_without_prs_is_none():
    event = {"check_run": {"name": "pytest", "pull_requests": []}}
    assert _pr_number_from_event(event, "check_run") is None


def test_pr_number_from_pull_request_event():
    event = {"pull_request": {"number": 7}}
    assert _pr_number_from_event(event, "pull_request") == 7


def test_pr_number_from_issue_comment_on_pr():
    event = {"issue": {"number": 12, "pull_request": {"url": "x"}}}
    assert _pr_number_from_event(event, "issue_comment") == 12


def test_pr_number_from_issue_comment_not_pr_is_none():
    event = {"issue": {"number": 12}}  # no pull_request key
    assert _pr_number_from_event(event, "issue_comment") is None


def test_own_workflow_check_names_present():
    # The dispatcher must know to ignore its own check-run completion.
    assert "dispatch" in OWN_WORKFLOW_CHECK_NAMES


# ---- #4: only dispatcher-bot-authored verdicts count -----------------------

class _FakeAPI:
    def __init__(self, comments):
        self._comments = comments

    def list_pr_comments(self, pr_number):
        return self._comments


def _verdict_comment(author_login, reviewer, head_sha, diff_sha, *, sign_secret=SECRET):
    v = build_verdict(
        reviewer=reviewer,
        verdict=VERDICT_APPROVE,
        pr_number=5,
        head_sha=head_sha,
        diff_sha256=diff_sha,
        tier="routine",
        round_=1,
    )
    signature = (
        compute_verdict_signature(v, sign_secret) if sign_secret else None
    )
    body = "Reasoning here.\n\n" + v.to_yaml_block(signature=signature)
    return PRComment(
        id=1,
        body=body,
        author_login=author_login,
        author_id=1,
        created_at="2026-06-09T00:00:00Z",
    )


def test_forged_human_verdict_is_ignored():
    head = "abc1234deadbeef"
    diff = compute_diff_sha256("some diff")
    comments = [
        # A malicious human pastes a perfectly-formed, even validly-signed
        # block (they cannot actually sign without the secret, but even if
        # they did, author check rejects them).
        _verdict_comment("evil-human", "gpt", head, diff),
    ]
    api = _FakeAPI(comments)
    verdicts = _trusted_existing_verdicts(api, pr_number=5, verdict_secret=SECRET)
    assert verdicts == []  # forged comment does not count


def test_unsigned_bot_verdict_is_ignored():
    # GPT review #5: another Actions workflow posts as github-actions[bot]
    # but cannot sign. An unsigned bot verdict must NOT count.
    head = "abc1234deadbeef"
    diff = compute_diff_sha256("some diff")
    comments = [
        _verdict_comment(DISPATCHER_BOT_LOGIN, "gpt", head, diff, sign_secret=None),
    ]
    api = _FakeAPI(comments)
    verdicts = _trusted_existing_verdicts(api, pr_number=5, verdict_secret=SECRET)
    assert verdicts == []


def test_wrong_signature_bot_verdict_is_ignored():
    # A bot verdict signed with the wrong secret must NOT count.
    head = "abc1234deadbeef"
    diff = compute_diff_sha256("some diff")
    comments = [
        _verdict_comment(DISPATCHER_BOT_LOGIN, "gpt", head, diff, sign_secret=WRONG_SECRET),
    ]
    api = _FakeAPI(comments)
    verdicts = _trusted_existing_verdicts(api, pr_number=5, verdict_secret=SECRET)
    assert verdicts == []


def test_signed_dispatcher_bot_verdict_is_counted():
    head = "abc1234deadbeef"
    diff = compute_diff_sha256("some diff")
    comments = [
        _verdict_comment(DISPATCHER_BOT_LOGIN, "gpt", head, diff),
    ]
    api = _FakeAPI(comments)
    verdicts = _trusted_existing_verdicts(api, pr_number=5, verdict_secret=SECRET)
    assert len(verdicts) == 1
    assert verdicts[0].reviewer == "gpt"


def test_mixed_comments_only_signed_bot_counts():
    head = "abc1234deadbeef"
    diff = compute_diff_sha256("some diff")
    comments = [
        _verdict_comment("evil-human", "claude", head, diff),
        _verdict_comment(DISPATCHER_BOT_LOGIN, "claude", head, diff, sign_secret=None),
        _verdict_comment(DISPATCHER_BOT_LOGIN, "gpt", head, diff),
    ]
    api = _FakeAPI(comments)
    verdicts = _trusted_existing_verdicts(api, pr_number=5, verdict_secret=SECRET)
    assert len(verdicts) == 1
    assert verdicts[0].reviewer == "gpt"


def test_no_secret_means_nothing_counts():
    # Fail-safe: with no secret configured, no verdict can count, so the
    # dispatcher can never falsely converge.
    head = "abc1234deadbeef"
    diff = compute_diff_sha256("some diff")
    comments = [_verdict_comment(DISPATCHER_BOT_LOGIN, "gpt", head, diff)]
    api = _FakeAPI(comments)
    verdicts = _trusted_existing_verdicts(api, pr_number=5, verdict_secret="")
    assert verdicts == []


def test_dispatcher_bot_login_constant():
    # Lock the trusted author identity so a refactor can't silently widen it.
    assert DISPATCHER_BOT_LOGIN == "github-actions[bot]"
