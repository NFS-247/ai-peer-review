"""Tests for the pure data-contract core."""

from front_door.app import viewmodels as vm


# ---- labels -----------------------------------------------------------------
def test_status_precedence():
    assert vm.status_from_labels(["dispatcher:secret-missing", "dispatcher:tier-routine"]) == "secret-missing"
    assert vm.status_from_labels(["dispatcher:paused", "dispatcher:escalated"]) == "paused"
    assert vm.status_from_labels(["dispatcher:escalated", "dispatcher:ready-for-merge"]) == "escalated"
    assert vm.status_from_labels(["dispatcher:ready-for-merge"]) == "ready"
    assert vm.status_from_labels(["dispatcher:tier-high_stakes", "dispatcher:round-3"]) == "reviewing"
    assert vm.status_from_labels(["something-else"]) == "unknown"


def test_tier_and_round_from_labels():
    labels = ["dispatcher:tier-high_stakes", "dispatcher:round-7"]
    assert vm.tier_from_labels(labels) == "high_stakes"
    assert vm.round_from_labels(labels) == 7
    assert vm.tier_from_labels(["x"]) is None
    assert vm.round_from_labels(["dispatcher:round-notanint"]) is None


# ---- state comment ----------------------------------------------------------
def _state_comment(cost):
    return {
        "body": (
            "<!-- tradewatcher-dispatcher-state -->\n\n"
            "```tradewatcher-dispatcher-state\n"
            f'{{"cumulative_cost_usd": {cost}, "ci_fix_attempts": 0}}\n'
            "```\n"
        )
    }


def test_parse_state_and_cost():
    comments = [{"body": "unrelated"}, _state_comment(5.8)]
    state = vm.parse_state(comments)
    assert vm.cost_from_state(state) == 5.8
    # absent -> 0
    assert vm.cost_from_state(vm.parse_state([{"body": "nope"}])) == 0.0


# ---- verdicts ---------------------------------------------------------------
def _verdict_comment(reviewer, verdict, rnd, emitted):
    return {"body": (
        "AI review\n```tradewatcher-verdict\n"
        f"reviewer: {reviewer}\nverdict: {verdict}\npr_number: 114\n"
        "head_sha: abcdef1\ndiff_sha256: " + "a" * 64 + "\n"
        f"tier: high_stakes\nround: {rnd}\nemitted_at: {emitted}\nsignature: deadbeef\n```\n"
    )}


def test_latest_verdicts_keeps_newest_per_reviewer():
    comments = [
        _verdict_comment("claude", "request_changes", 1, "2026-06-10T01:00:00Z"),
        _verdict_comment("claude", "approve", 3, "2026-06-10T03:00:00Z"),
        _verdict_comment("gpt", "request_changes", 3, "2026-06-10T03:00:05Z"),
    ]
    assert vm.latest_verdicts(comments) == {"claude": "approve", "gpt": "request_changes"}


def test_latest_verdicts_empty_when_none():
    assert vm.latest_verdicts([{"body": "no verdict here"}]) == {}


# ---- spend window -----------------------------------------------------------
NOW = 1_000_000.0


def _ledger(events):
    import json
    return ("<!-- tradewatcher-dispatcher-global-spend -->\n"
            "```tradewatcher-global-spend\n" + json.dumps({"events": events}) + "\n```\n")


def test_spend_from_ledger_windows_and_splits():
    body = _ledger([
        {"ts": NOW - 100, "cost": 1.0, "by": {"claude": 0.6, "gpt": 0.4}},
        {"ts": NOW - 200, "cost": 0.5, "by": {"gemini": 0.5}},
        {"ts": NOW - vm.WINDOW_SECONDS - 1, "cost": 9.0, "by": {"gpt": 9.0}},  # too old
    ])
    total, by = vm.spend_from_ledger(body, now_ts=NOW)
    assert total == 1.5
    assert by == {"claude": 0.6, "gpt": 0.4, "gemini": 0.5}


def test_spend_from_ledger_empty():
    assert vm.spend_from_ledger("", now_ts=NOW) == (0.0, {})


# ---- assembly ---------------------------------------------------------------
def test_build_board_row_and_inbox_ordering():
    pr = {"number": 114, "title": "Phase 4a", "html_url": "http://x/114", "updated_at": "t"}
    row = vm.build_board_row(
        repo="NFS-247/StockTrader", pr=pr,
        labels=["dispatcher:tier-high_stakes", "dispatcher:round-10", "dispatcher:escalated"],
        comments=[_state_comment(5.8), _verdict_comment("claude", "approve", 10, "2026-06-10T03:00:00Z")],
    )
    assert row.status == "escalated"
    assert row.tier == "high_stakes" and row.round == 10
    assert row.cost_usd == 5.8
    assert row.reviewers == {"claude": "approve"}

    ready = vm.build_board_row(
        repo="r", pr={"number": 5, "title": "t", "html_url": "u"},
        labels=["dispatcher:tier-routine", "dispatcher:ready-for-merge"], comments=[])
    reviewing = vm.build_board_row(
        repo="r", pr={"number": 6, "title": "t", "html_url": "u"},
        labels=["dispatcher:tier-routine"], comments=[])
    inbox = vm.inbox_from_rows([ready, reviewing, row])
    # escalated first, then ready; 'reviewing' is not actionable.
    assert [(i.number, i.status) for i in inbox] == [(114, "escalated"), (5, "ready")]
