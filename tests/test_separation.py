"""Unit tests for the separation domain logic."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.separation import (
    DEFAULT_MATRIX,
    Flight,
    build_matrix,
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
