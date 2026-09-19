"""Persistence helpers for batches, flights, violations and trial items."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .separation import ClosureWindow, Flight, TrialItem, Violation
from .timeutils import ensure_utc


def new_batch_id() -> str:
    return uuid4().hex


def _iso(dt: datetime) -> str:
    return ensure_utc(dt).isoformat()


def save_batch(
    conn: sqlite3.Connection,
    *,
    mode: str,
    matrix: dict[tuple[str, str], int],
    runway_payloads: dict[str, list[Flight]],
    closures_by_runway: dict[str, list[ClosureWindow]] | None = None,
    violations_by_runway: dict[str, list[Violation]] | None = None,
    closure_hits_by_runway: dict[str, list] | None = None,
    trial_by_runway: dict[str, list[TrialItem]] | None = None,
) -> str:
    batch_id = new_batch_id()
    created_at = datetime.now(timezone.utc).isoformat()
    matrix_json = json.dumps(
        {f"{leader}->{follower}": seconds for (leader, follower), seconds in matrix.items()},
        sort_keys=True,
    )
    conn.execute(
        "INSERT INTO batches (batch_id, mode, created_at, matrix_json) VALUES (?, ?, ?, ?)",
        (batch_id, mode, created_at, matrix_json),
    )

    for runway, flights in runway_payloads.items():
        for position, flight in enumerate(flights, start=1):
            conn.execute(
                "INSERT INTO flights (batch_id, runway, position, flight_no, scheduled_off, wake)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    position,
                    flight.flight_no,
                    _iso(flight.scheduled_off),
                    flight.wake,
                ),
            )

    for runway, windows in (closures_by_runway or {}).items():
        for position, w in enumerate(windows, start=1):
            conn.execute(
                "INSERT INTO closures (batch_id, runway, position, start_time, end_time)"
                " VALUES (?, ?, ?, ?, ?)",
                (batch_id, runway, position, _iso(w.start), _iso(w.end)),
            )

    for runway, hits in (closure_hits_by_runway or {}).items():
        for hit in hits:
            conn.execute(
                "INSERT INTO closure_hits (batch_id, runway, flight_no, wake, flight_time,"
                " flight_position, window_start, window_end)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    hit.flight_no,
                    hit.wake,
                    _iso(hit.scheduled_time),
                    hit.position,
                    _iso(hit.window_start),
                    _iso(hit.window_end),
                ),
            )

    for runway, violations in (violations_by_runway or {}).items():
        for v in violations:
            conn.execute(
                "INSERT INTO violations (batch_id, runway, leader_position, follower_position,"
                " leader_no, follower_no, leader_wake, follower_wake, leader_time, follower_time,"
                " required_seconds, actual_seconds, shortfall_seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    v.leader_position,
                    v.follower_position,
                    v.leader_no,
                    v.follower_no,
                    v.leader_wake,
                    v.follower_wake,
                    _iso(v.leader_time),
                    _iso(v.follower_time),
                    v.required_seconds,
                    v.actual_seconds,
                    v.shortfall_seconds,
                ),
            )

    for runway, items in (trial_by_runway or {}).items():
        for item in items:
            conn.execute(
                "INSERT INTO trial_items (batch_id, runway, position, flight_no, wake,"
                " scheduled_off, adjusted_off, delay_seconds, triggered_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    item.position,
                    item.flight_no,
                    item.wake,
                    _iso(item.original_time),
                    _iso(item.adjusted_time),
                    item.delay_seconds,
                    item.triggered_by,
                ),
            )
            for push_order, push in enumerate(item.window_pushes, start=1):
                conn.execute(
                    "INSERT INTO trial_window_pushes (batch_id, runway, flight_no,"
                    " flight_position, push_order, window_start, window_end, reason)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        runway,
                        item.flight_no,
                        item.position,
                        push_order,
                        _iso(push.window_start),
                        _iso(push.window_end),
                        push.reason,
                    ),
                )

    return batch_id


def load_batch(conn: sqlite3.Connection, batch_id: str) -> dict | None:
    """Reconstruct a stored batch in the same shape as the POST responses."""
    row = conn.execute(
        "SELECT batch_id, mode, created_at, matrix_json FROM batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        return None

    flat = {
        tuple(pair.split("->", 1)): seconds
        for pair, seconds in json.loads(row["matrix_json"]).items()
    }
    matrix: dict[str, dict[str, int]] = {}
    for (leader, follower), seconds in flat.items():
        matrix.setdefault(leader, {})[follower] = seconds

    flight_rows = conn.execute(
        "SELECT runway, position, flight_no FROM flights"
        " WHERE batch_id = ? ORDER BY runway, position",
        (batch_id,),
    ).fetchall()
    order_by_runway: dict[str, list[str]] = {}
    for fr in flight_rows:
        order_by_runway.setdefault(fr["runway"], []).append(fr["flight_no"])

    closure_rows = conn.execute(
        "SELECT runway, position, start_time, end_time FROM closures"
        " WHERE batch_id = ? ORDER BY runway, position",
        (batch_id,),
    ).fetchall()
    closures_by_runway: dict[str, list[dict]] = {}
    for cr in closure_rows:
        closures_by_runway.setdefault(cr["runway"], []).append(
            {"start": cr["start_time"], "end": cr["end_time"]}
        )

    if row["mode"] == "validate":
        vrows = conn.execute(
            "SELECT * FROM violations WHERE batch_id = ? ORDER BY runway, leader_position",
            (batch_id,),
        ).fetchall()
        runways = []
        violations_by_runway: dict[str, list[dict]] = {}
        for vr in vrows:
            violations_by_runway.setdefault(vr["runway"], []).append(
                {
                    "leader": vr["leader_no"],
                    "follower": vr["follower_no"],
                    "leader_wake": vr["leader_wake"],
                    "follower_wake": vr["follower_wake"],
                    "leader_scheduled_off": vr["leader_time"],
                    "follower_scheduled_off": vr["follower_time"],
                    "required_seconds": vr["required_seconds"],
                    "actual_seconds": vr["actual_seconds"],
                    "shortfall_seconds": vr["shortfall_seconds"],
                    "leader_position": vr["leader_position"],
                    "follower_position": vr["follower_position"],
                }
            )
        hrows = conn.execute(
            "SELECT * FROM closure_hits WHERE batch_id = ?"
            " ORDER BY runway, flight_position",
            (batch_id,),
        ).fetchall()
        hits_by_runway: dict[str, list[dict]] = {}
        for hr in hrows:
            hits_by_runway.setdefault(hr["runway"], []).append(
                {
                    "flight_no": hr["flight_no"],
                    "wake": hr["wake"],
                    "scheduled_off": hr["flight_time"],
                    "position": hr["flight_position"],
                    "window_start": hr["window_start"],
                    "window_end": hr["window_end"],
                }
            )
        for runway in order_by_runway:
            violations = violations_by_runway.get(runway, [])
            hits = hits_by_runway.get(runway, [])
            runways.append(
                {
                    "runway": runway,
                    "order": order_by_runway[runway],
                    "total": len(order_by_runway[runway]),
                    "violation_count": len(violations),
                    "violations": violations,
                    "closures": closures_by_runway.get(runway, []),
                    "closure_hit_count": len(hits),
                    "closure_hits": hits,
                }
            )
        return {
            "batch_id": row["batch_id"],
            "mode": "validate",
            "created_at": row["created_at"],
            "matrix": matrix,
            "total_runways": len(runways),
            "total_flights": sum(len(o) for o in order_by_runway.values()),
            "total_violations": sum(len(v) for v in violations_by_runway.values()),
            "total_closure_hits": sum(len(h) for h in hits_by_runway.values()),
            "runways": runways,
        }

    # trial
    trows = conn.execute(
        "SELECT * FROM trial_items WHERE batch_id = ? ORDER BY runway, position",
        (batch_id,),
    ).fetchall()
    prows = conn.execute(
        "SELECT * FROM trial_window_pushes WHERE batch_id = ?"
        " ORDER BY runway, flight_position, push_order",
        (batch_id,),
    ).fetchall()
    pushes_by_flight: dict[tuple[str, str], list[dict]] = {}
    for pr in prows:
        pushes_by_flight.setdefault((pr["runway"], pr["flight_no"]), []).append(
            {
                "window_start": pr["window_start"],
                "window_end": pr["window_end"],
                "reason": pr["reason"],
            }
        )
    items_by_runway: dict[str, list[dict]] = {}
    for tr in trows:
        pushes = pushes_by_flight.get((tr["runway"], tr["flight_no"]), [])
        items_by_runway.setdefault(tr["runway"], []).append(
            {
                "position": tr["position"],
                "flight_no": tr["flight_no"],
                "wake": tr["wake"],
                "scheduled_off": tr["scheduled_off"],
                "adjusted_off": tr["adjusted_off"],
                "delay_seconds": tr["delay_seconds"],
                "triggered_by": tr["triggered_by"],
                "window_push_count": len(pushes),
                "window_pushes": pushes,
            }
        )
    runways = []
    total_delayed = 0
    total_delay = 0
    for runway in order_by_runway:
        items = items_by_runway.get(runway, [])
        delayed = [i for i in items if i["delay_seconds"] > 0]
        rw_delay = sum(i["delay_seconds"] for i in items)
        max_delay = max((i["delay_seconds"] for i in items), default=0)
        total_delayed += len(delayed)
        total_delay += rw_delay
        runways.append(
            {
                "runway": runway,
                "order": order_by_runway[runway],
                "total": len(items),
                "delayed_count": len(delayed),
                "total_delay_seconds": rw_delay,
                "max_delay_seconds": max_delay,
                "closures": closures_by_runway.get(runway, []),
                "items": items,
            }
        )
    return {
        "batch_id": row["batch_id"],
        "mode": "trial",
        "created_at": row["created_at"],
        "matrix": matrix,
        "total_runways": len(runways),
        "total_flights": sum(len(o) for o in order_by_runway.values()),
        "total_delayed": total_delayed,
        "total_delay_seconds": total_delay,
        "runways": runways,
    }
