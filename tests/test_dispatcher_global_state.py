"""Tests for scripts.dispatcher.global_state (24h spend window math).

Covers GPT review #2 on PR #74: the cost ceiling must be a 24-hour global
window, not per-PR. The window math is pure and tested without GitHub.
"""

from scripts.dispatcher.global_state import (
    WINDOW_SECONDS,
    prune,
    sum_recent,
)


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
