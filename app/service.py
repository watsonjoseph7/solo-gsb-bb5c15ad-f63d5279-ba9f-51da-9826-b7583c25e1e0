"""Application service: request -> domain objects -> persistence payloads."""

from __future__ import annotations

from .schemas import WakeRequest, matrix_to_nested
from .separation import (
    Flight,
    TrialItem,
    Violation,
    build_matrix,
    find_violations,
    sort_flights,
    trial_reschedule,
)
from .timeutils import ensure_utc


def _build_runway_flights(req: WakeRequest) -> dict[str, list[Flight]]:
    """Normalise every flight to UTC and return per-runway sorted lists."""
    payloads: dict[str, list[Flight]] = {}
    for rw in req.runways:
        flights = [
            Flight(
                flight_no=f.flight_no,
                scheduled_off=ensure_utc(f.scheduled_off),
                wake=f.wake.value,
            )
            for f in rw.flights
        ]
        payloads[rw.runway] = sort_flights(flights)
    return payloads


def _matrix_from_request(req: WakeRequest) -> dict[tuple[str, str], int]:
    if req.matrix is None:
        return build_matrix(None)
    return build_matrix(
        {(e.leader.value, e.follower.value): e.seconds for e in req.matrix}
    )


def run_validate(req: WakeRequest) -> tuple[
    dict[str, list[Flight]],
    dict[tuple[str, str], int],
    dict[str, list[Violation]],
]:
    payloads = _build_runway_flights(req)
    matrix = _matrix_from_request(req)
    violations = {
        runway: find_violations(flights, matrix)
        for runway, flights in payloads.items()
    }
    return payloads, matrix, violations


def run_trial(req: WakeRequest) -> tuple[
    dict[str, list[Flight]],
    dict[tuple[str, str], int],
    dict[str, list[TrialItem]],
]:
    payloads = _build_runway_flights(req)
    matrix = _matrix_from_request(req)
    trials = {
        runway: trial_reschedule(flights, matrix)
        for runway, flights in payloads.items()
    }
    return payloads, matrix, trials


def nested_matrix(matrix: dict[tuple[str, str], int]) -> dict[str, dict[str, int]]:
    return matrix_to_nested(matrix)
