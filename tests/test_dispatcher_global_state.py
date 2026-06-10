"""Tests for scripts.dispatcher.global_state (24h spend window math).

Covers GPT review #2 on PR #74: the cost ceiling must be a 24-hour global
window, not per-PR. The window math is pure and tested without GitHub.
"""

from scripts.dispatcher.global_state import (
    WINDOW_SECONDS,
    budget_warning_due,
    prune,
    sum_recent,
)


def test_budget_warning_fires_on_the_crossing_round():
    # 80% of 20 = 16; this round took spend from 15 -> 17, crossing it.
    assert budget_warning_due(total_before=15.0, total_after=17.0, ceiling=20.0, warn_fraction=0.8) is True


def test_budget_warning_silent_once_already_in_band():
    # Already past 80% before this round -> no repeat ping.
    assert budget_warning_due(total_before=17.0, total_after=18.0, ceiling=20.0, warn_fraction=0.8) is False


def test_budget_warning_silent_when_over_ceiling():
    # At/over the hard ceiling is the pause/escalation path, not a pre-warning.
    assert budget_warning_due(total_before=18.0, total_after=21.0, ceiling=20.0, warn_fraction=0.8) is False


def test_budget_warning_disabled_by_zero_fraction_or_ceiling():
    assert budget_warning_due(total_before=0.0, total_after=19.0, ceiling=20.0, warn_fraction=0.0) is False
    assert budget_warning_due(total_before=0.0, total_after=19.0, ceiling=0.0, warn_fraction=0.8) is False


NOW = 1_000_000.0


def test_sum_recent_includes_in_window():
    events = [
        {"ts": NOW - 100, "cost": 1.0},
        {"ts": NOW - 200, "cost": 2.0},
    ]
    assert sum_recent(events, now_ts=NOW) == 3.0


def test_sum_recent_excludes_out_of_window():
    events = [
        {"ts": NOW - 100, "cost": 1.0},
        {"ts": NOW - WINDOW_SECONDS - 1, "cost": 99.0},  # too old
    ]
    assert sum_recent(events, now_ts=NOW) == 1.0


def test_sum_recent_boundary_inclusive():
    events = [{"ts": NOW - WINDOW_SECONDS, "cost": 5.0}]  # exactly at edge
    assert sum_recent(events, now_ts=NOW) == 5.0


def test_sum_recent_ignores_malformed():
    events = [
        {"ts": NOW - 10, "cost": 1.0},
        {"ts": "bad", "cost": "bad"},
        {"cost": 2.0},  # missing ts -> ts=0 -> out of window
    ]
    assert sum_recent(events, now_ts=NOW) == 1.0


def test_prune_drops_old_events():
    events = [
        {"ts": NOW - 100, "cost": 1.0},
        {"ts": NOW - WINDOW_SECONDS - 5, "cost": 9.0},
    ]
    pruned = prune(events, now_ts=NOW)
    assert len(pruned) == 1
    assert pruned[0]["cost"] == 1.0


def test_prune_keeps_all_in_window():
    events = [
        {"ts": NOW - 1, "cost": 1.0},
        {"ts": NOW - 2, "cost": 2.0},
    ]
    pruned = prune(events, now_ts=NOW)
    assert len(pruned) == 2


def test_empty_events_sum_zero():
    assert sum_recent([], now_ts=NOW) == 0.0
