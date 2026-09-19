"""Core wake-separation domain logic.

The service operates on *time in seconds* so the separation matrix (defined
in seconds) can be applied with plain integer arithmetic, no float rounding.

Wake turbulence categories follow the four-class scheme:

``L``  LIGHT    – light aircraft (e.g. PA28, C152)
``M``  MEDIUM   – medium aircraft (most airliners below 136 t)
``H``  HEAVY    – heavy aircraft (>= 136 t MTOW)
``J``  SUPER    – super aircraft (e.g. A380)

Only *adjacent* pairs on the same runway are considered, matching the
operational practice for departures on a single runway: the leader of a pair
is the flight immediately ahead in the departure order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .timeutils import to_epoch_seconds


class Wake(str, Enum):
    LIGHT = "L"
    MEDIUM = "M"
    HEAVY = "H"
    SUPER = "J"


# Default leader -> follower minimum separation in seconds.
# ICAO-style departure values (recat categories map loosely to this matrix).
#   LIGHT leader  : 100 s behind light, otherwise 60 s
#   MEDIUM leader : 100 s behind light, otherwise 60 s
#   HEAVY leader  : 120 s behind medium, 100 s behind heavy, 100 s light
#   SUPER leader  : 180 s behind heavy/medium, 180 s light, 120 s super
# Values are intentionally simple defaults; a request may override the whole
# matrix, in which case any omitted leader/follower pair defaults to zero
# (i.e. no required radar minimum for that pair).
DEFAULT_MATRIX: dict[str, dict[str, int]] = {
    "L": {"L": 100, "M": 60, "H": 60, "J": 60},
    "M": {"L": 100, "M": 60, "H": 60, "J": 60},
    "H": {"L": 100, "M": 120, "H": 100, "J": 60},
    "J": {"L": 180, "M": 180, "H": 180, "J": 120},
}


@dataclass(frozen=True, slots=True)
class Flight:
    """A flight plan item after timezone normalisation."""

    flight_no: str
    scheduled_off: datetime  # always tz-aware, converted to UTC
    wake: str

    @property
    def t(self) -> int:
        return to_epoch_seconds(self.scheduled_off)

    def sort_key(self) -> tuple[int, str]:
        """Stable ordering key: scheduled time, then flight number."""
        return self.t, self.flight_no


@dataclass(frozen=True, slots=True)
class Violation:
    leader_no: str
    follower_no: str
    leader_wake: str
    follower_wake: str
    leader_time: datetime
    follower_time: datetime
    required_seconds: int
    actual_seconds: int
    shortfall_seconds: int
    leader_position: int
    follower_position: int


@dataclass(frozen=True, slots=True)
class ClosureWindow:
    """A half-open ``[start, end)`` temporary runway closure (UTC seconds)."""

    start: datetime  # always tz-aware, converted to UTC
    end: datetime  # always tz-aware, converted to UTC

    @property
    def start_t(self) -> int:
        return to_epoch_seconds(self.start)

    @property
    def end_t(self) -> int:
        return to_epoch_seconds(self.end)

    def contains(self, t: int) -> bool:
        """Half-open membership: ``start <= t < end``."""
        return self.start_t <= t < self.end_t


@dataclass(frozen=True, slots=True)
class ClosureHit:
    """A flight whose *scheduled* departure time falls inside a closure."""

    flight_no: str
    wake: str
    scheduled_time: datetime
    position: int
    window_start: datetime
    window_end: datetime


def sort_windows(windows: list[ClosureWindow]) -> list[ClosureWindow]:
    """Sort closure windows by start time (they must already be non-overlapping)."""
    return sorted(windows, key=lambda w: (w.start_t, w.end_t))


def find_closure_hits(
    flights: list[Flight], windows: list[ClosureWindow]
) -> list[ClosureHit]:
    """List flights whose scheduled time lies within any ``[start, end)`` window.

    Flights are walked in their stable departure order; windows are expected
    sorted and non-overlapping, so a simple advancing pointer suffices.
    """
    ordered = sort_flights(flights)
    ws = sort_windows(windows)
    hits: list[ClosureHit] = []
    wi = 0
    for position, flight in enumerate(ordered, start=1):
        while wi < len(ws) and ws[wi].end_t <= flight.t:
            wi += 1
        if wi < len(ws) and ws[wi].contains(flight.t):
            hits.append(
                ClosureHit(
                    flight_no=flight.flight_no,
                    wake=flight.wake,
                    scheduled_time=flight.scheduled_off,
                    position=position,
                    window_start=ws[wi].start,
                    window_end=ws[wi].end,
                )
            )
    return hits


def advance_past_windows(
    t: int, windows: list[ClosureWindow]
) -> tuple[int, list[ClosureWindow]]:
    """Push instant ``t`` forward until it lies outside every closure.

    Each time ``t`` falls in a ``[start, end)`` window it jumps to that
    window's ``end``; landing inside a *later* window triggers another jump,
    so window chains are resolved by iteration. Returns the earliest safe
    instant ``>= t`` and the windows actually traversed (in order).
    """
    hit_windows: list[ClosureWindow] = []
    for w in windows:  # sorted, non-overlapping
        if w.end_t <= t:
            continue
        if t < w.start_t:
            break
        # start <= t < end: t is inside this window
        t = w.end_t
        hit_windows.append(w)
    return t, hit_windows


@dataclass(slots=True)
class TrialItem:
    flight_no: str
    wake: str
    original_time: datetime
    adjusted_time: datetime
    delay_seconds: int
    position: int
    triggered_by: str | None  # flight number of the leader that forced the pushback
    window_pushes: list["WindowPush"] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WindowPush:
    """One closure-window jump performed while rescheduling a flight."""

    window_start: datetime
    window_end: datetime
    reason: str  # "scheduled" — original time inside the window; "cascade" — pushed in by a delay


def build_matrix(
    entries: dict[tuple[str, str], int] | None,
) -> dict[tuple[str, str], int]:
    """Return a ``(leader, follower) -> seconds`` lookup.

    ``entries`` is the client-supplied override. When omitted/empty the built-in
    :data:`DEFAULT_MATRIX` is used; otherwise every unspecified pair is zero
    (no minimum for that pair).
    """
    if not entries:
        return {
            (leader, follower): seconds
            for leader, followers in DEFAULT_MATRIX.items()
            for follower, seconds in followers.items()
        }
    return dict(entries)


def required_gap(
    matrix: dict[tuple[str, str], int], leader_wake: str, follower_wake: str
) -> int:
    return matrix.get((leader_wake, follower_wake), 0)


def sort_flights(flights: list[Flight]) -> list[Flight]:
    """Stable sort by (scheduled time, flight number)."""
    return sorted(flights, key=Flight.sort_key)


def find_violations(
    flights: list[Flight], matrix: dict[tuple[str, str], int]
) -> list[Violation]:
    """List violations between every adjacent pair of the (sorted) sequence."""
    ordered = sort_flights(flights)
    violations: list[Violation] = []
    for i in range(len(ordered) - 1):
        leader, follower = ordered[i], ordered[i + 1]
        required = required_gap(matrix, leader.wake, follower.wake)
        actual = follower.t - leader.t
        if actual < required:
            violations.append(
                Violation(
                    leader_no=leader.flight_no,
                    follower_no=follower.flight_no,
                    leader_wake=leader.wake,
                    follower_wake=follower.wake,
                    leader_time=leader.scheduled_off,
                    follower_time=follower.scheduled_off,
                    required_seconds=required,
                    actual_seconds=actual,
                    shortfall_seconds=required - actual,
                    leader_position=i + 1,
                    follower_position=i + 2,
                )
            )
    return violations


def trial_reschedule(
    flights: list[Flight],
    matrix: dict[tuple[str, str], int],
    windows: list[ClosureWindow] | None = None,
) -> list[TrialItem]:
    """Greedily push flights back to the earliest safe time, keeping order.

    The first flight keeps its scheduled time unless a closure window covers
    it. Every following flight keeps at least the matrix gap behind the
    (possibly already delayed) flight directly ahead of it, so delays cascade
    to successors. After every push (wake gap *or* closure) the candidate time
    is re-checked against all closure windows: a push that lands inside a
    later window jumps again to that window's end, iterating until the time is
    both gap-safe and outside every ``[start, end)`` closure. Original
    schedules are never rewritten; only :class:`TrialItem` proposals are
    returned.
    """
    ordered = sort_flights(flights)
    ws = sort_windows(windows or [])
    items: list[TrialItem] = []
    prev: Flight | None = None
    prev_adjusted_t = 0
    for position, flight in enumerate(ordered, start=1):
        adjusted_t = flight.t
        triggered_by: str | None = None
        pushes: list[WindowPush] = []

        # 1) The flight's own scheduled time must be outside every closure.
        #    Jumps done here (including across several adjacent windows) are
        #    caused by the schedule, not by the wake cascade.
        adjusted_t, own_hits = advance_past_windows(adjusted_t, ws)
        for w in own_hits:
            pushes.append(WindowPush(window_start=w.start, window_end=w.end, reason="scheduled"))

        # 2) Honour the wake gap behind the (rescheduled) direct leader, then
        #    re-check closures: a push that lands inside a later window jumps
        #    to its end (reason "cascade"), iterating until the time is both
        #    gap-safe and outside every window.
        if prev is not None:
            required = required_gap(matrix, prev.wake, flight.wake)
            while True:
                earliest = prev_adjusted_t + required
                if adjusted_t < earliest:
                    adjusted_t = earliest
                    triggered_by = prev.flight_no
                jumped, crossed = advance_past_windows(adjusted_t, ws)
                if jumped == adjusted_t:
                    break
                adjusted_t = jumped
                for w in crossed:
                    pushes.append(
                        WindowPush(window_start=w.start, window_end=w.end, reason="cascade")
                    )

        items.append(
            TrialItem(
                flight_no=flight.flight_no,
                wake=flight.wake,
                original_time=flight.scheduled_off,
                adjusted_time=datetime.fromtimestamp(adjusted_t, tz=timezone.utc),
                delay_seconds=adjusted_t - flight.t,
                position=position,
                triggered_by=triggered_by,
                window_pushes=pushes,
            )
        )
        prev = flight
        prev_adjusted_t = adjusted_t
    return items
