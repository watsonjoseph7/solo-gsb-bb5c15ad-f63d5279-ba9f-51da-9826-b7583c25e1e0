"""Unit tests for the separation domain logic."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.separation import (
    DEFAULT_MATRIX,
    ClosureWindow,
    Flight,
    build_matrix,
    find_closure_hits,
    find_violations,
    required_gap,
    sort_flights,
    trial_reschedule,
)

UTC = timezone.utc


def dt(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def make(no: str, t: int, wake: str) -> Flight:
    return Flight(flight_no=no, scheduled_off=dt(t), wake=wake)


def win(start: int, end: int) -> ClosureWindow:
    return ClosureWindow(start=dt(start), end=dt(end))


def test_stable_sort_by_time_then_flight_no():
    flights = [
        make("BBB", 200, "M"),
        make("AAA", 100, "H"),
        make("CCC", 100, "L"),  # same time as AAA -> flight_no tiebreak
    ]
    assert [f.flight_no for f in sort_flights(flights)] == ["AAA", "CCC", "BBB"]
    # input order unchanged (immutable objects, new list)
    assert flights[0].flight_no == "BBB"


def test_default_matrix_contents():
    matrix = build_matrix(None)
    assert matrix[("J", "M")] == DEFAULT_MATRIX["J"]["M"] == 180
    assert required_gap(matrix, "H", "M") == 120


def test_custom_matrix_unknown_pair_is_zero():
    matrix = build_matrix({("H", "M"): 90})
    assert required_gap(matrix, "H", "M") == 90
    assert required_gap(matrix, "J", "L") == 0


def test_find_violations_flags_only_adjacent_breaches():
    # H leader (100-> next M needs 120s); M->M needs 60s
    flights = [
        make("CA101", 0, "H"),
        make("CA102", 60, "M"),    # gap 60 < 120 -> violation
        make("CA103", 130, "M"),   # gap 70 >= 60 -> ok
    ]
    violations = find_violations(flights, build_matrix(None))
    assert len(violations) == 1
    v = violations[0]
    assert (v.leader_no, v.follower_no) == ("CA101", "CA102")
    assert v.required_seconds == 120
    assert v.actual_seconds == 60
    assert v.shortfall_seconds == 60
    assert (v.leader_position, v.follower_position) == (1, 2)


def test_no_violation_when_exact_gap_met():
    flights = [make("CA101", 0, "H"), make("CA102", 120, "M")]
    assert find_violations(flights, build_matrix(None)) == []


def test_trial_cascades_delays_without_mutating_originals():
    # J(0) -> M needs 180; M -> M needs 60
    flights = [
        make("CA1", 0, "J"),
        make("CA2", 60, "M"),    # pushed to 180 (triggered by CA1), delay 120
        make("CA3", 200, "M"),   # must be >= 240 (triggered by CA2), delay 40
    ]
    items = trial_reschedule(flights, build_matrix(None))
    assert [i.flight_no for i in items] == ["CA1", "CA2", "CA3"]
    assert items[0].delay_seconds == 0
    assert items[0].triggered_by is None
    assert items[1].adjusted_time == dt(180)
    assert items[1].delay_seconds == 120
    assert items[1].triggered_by == "CA1"
    assert items[2].adjusted_time == dt(240)
    assert items[2].delay_seconds == 40
    assert items[2].triggered_by == "CA2"

    # original schedules are untouched
    assert flights[1].scheduled_off == dt(60)
    assert flights[2].scheduled_off == dt(200)


def test_trial_never_moves_flight_earlier():
    flights = [make("CA1", 1000, "M"), make("CA2", 5000, "M")]
    items = trial_reschedule(flights, build_matrix(None))
    assert items[1].delay_seconds == 0
    assert items[1].adjusted_time == dt(5000)


def test_trial_with_custom_matrix():
    matrix = build_matrix({("H", "H"): 300})
    flights = [make("CA1", 0, "H"), make("CA2", 100, "H")]
    items = trial_reschedule(flights, matrix)
    assert items[1].adjusted_time == dt(300)
    assert items[1].delay_seconds == 200
    assert items[1].triggered_by == "CA1"


def test_single_flight_has_no_violation_and_no_delay():
    flights = [make("SOLO", 42, "J")]
    assert find_violations(flights, build_matrix(None)) == []
    (item,) = trial_reschedule(flights, build_matrix(None))
    assert item.delay_seconds == 0 and item.triggered_by is None


# ---------- closure windows ----------


def test_half_open_window_boundary():
    w = win(100, 200)
    assert w.contains(100) is True   # start inclusive
    assert w.contains(199) is True
    assert w.contains(200) is False  # end exclusive


def test_find_closure_hits_matches_scheduled_times():
    flights = [make("A", 50, "M"), make("B", 100, "M"), make("C", 200, "M")]
    hits = find_closure_hits(flights, [win(100, 200)])
    assert [h.flight_no for h in hits] == ["B"]
    hit = hits[0]
    assert hit.position == 2
    assert hit.window_start == dt(100) and hit.window_end == dt(200)
    # exactly on the end is not a hit
    assert find_closure_hits([make("D", 200, "M")], [win(100, 200)]) == []


def test_trial_pushes_scheduled_closure_first_flight():
    items = trial_reschedule([make("SOLO", 50, "M")], build_matrix(None), [win(0, 100)])
    item = items[0]
    assert item.adjusted_time == dt(100)
    assert item.delay_seconds == 50
    assert item.triggered_by is None
    assert item.window_pushes[0].reason == "scheduled"


def test_trial_wake_push_into_window_is_cascade():
    # J->M requires 180s: F2 pushed 60 -> 180 which opens [180, 240)
    flights = [make("F1", 0, "J"), make("F2", 60, "M")]
    items = trial_reschedule(flights, build_matrix(None), [win(180, 240)])
    item = items[1]
    assert item.adjusted_time == dt(240)
    assert item.delay_seconds == 180
    assert item.triggered_by == "F1"
    assert [p.reason for p in item.window_pushes] == ["cascade"]


def test_trial_iterates_across_adjacent_windows():
    # own time 60 in [60,120) -> 120 at start of [120,180) -> 180
    flights = [make("A", 0, "M"), make("B", 60, "M")]
    items = trial_reschedule(flights, build_matrix(None), [win(60, 120), win(120, 180)])
    item = items[1]
    assert item.adjusted_time == dt(180)
    assert [p.reason for p in item.window_pushes] == ["scheduled", "scheduled"]


def test_trial_wake_cascade_across_two_later_windows():
    # own time 90 is clean; gap behind A@50 needs 110 -> in [100,150) -> 150 in [150,200) -> 200
    flights = [make("A", 50, "M"), make("B", 90, "M")]
    items = trial_reschedule(flights, build_matrix(None), [win(100, 150), win(150, 200)])
    item = items[1]
    assert item.adjusted_time == dt(200)
    assert item.delay_seconds == 110
    assert item.triggered_by == "A"
    assert [p.reason for p in item.window_pushes] == ["cascade", "cascade"]


def test_trial_scheduled_hit_then_wake_push_into_later_window():
    # F2 scheduled 100 in [100,160) -> 160; J->M gap needs 180 -> in [180,260) -> 260
    flights = [make("F1", 0, "J"), make("F2", 100, "M")]
    items = trial_reschedule(flights, build_matrix(None), [win(100, 160), win(180, 260)])
    item = items[1]
    assert item.adjusted_time == dt(260)
    assert item.delay_seconds == 160
    assert item.triggered_by == "F1"
    assert [p.reason for p in item.window_pushes] == ["scheduled", "cascade"]


def test_trial_closure_cascades_to_successors():
    # J@0 -> M needs 180; F2@100 in [100,300) -> 300;
    # F3@350 needs 60 behind F2@300 -> pushed to 360 (cascade, no window involved)
    flights = [make("F1", 0, "J"), make("F2", 100, "M"), make("F3", 350, "M")]
    items = trial_reschedule(flights, build_matrix(None), [win(100, 300)])
    assert items[1].adjusted_time == dt(300)
    assert items[2].adjusted_time == dt(360)
    assert items[2].triggered_by == "F2"
    assert items[2].window_pushes == []


def test_trial_departure_exactly_at_window_end_allowed():
    items = trial_reschedule([make("SOLO", 100, "M")], build_matrix(None), [win(0, 100)])
    assert items[0].delay_seconds == 0 and items[0].window_pushes == []


def test_trial_without_windows_unchanged_behaviour():
    flights = [make("CA1", 0, "J"), make("CA2", 60, "M")]
    items = trial_reschedule(flights, build_matrix(None))
    assert items[1].adjusted_time == dt(180)
    assert items[1].window_pushes == []
