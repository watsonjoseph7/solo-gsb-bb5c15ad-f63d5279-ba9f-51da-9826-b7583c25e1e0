"""End-to-end API tests (FastAPI TestClient + real SQLite file)."""

from __future__ import annotations


def _flight(no: str, off: str, wake: str) -> dict:
    return {"flight_no": no, "scheduled_off": off, "wake": wake}


VALIDATE_BODY = {
    "runways": [
        {
            "runway": "36L",
            "flights": [
                _flight("CA101", "2026-09-18T00:00:00Z", "H"),
                _flight("CA102", "2026-09-18T00:01:00Z", "M"),  # 60s < H->M 120s
                _flight("CA103", "2026-09-18T00:02:10Z", "M"),  # 70s >= M->M 60s
            ],
        },
        {
            "runway": "18R",
            "flights": [
                _flight("MU201", "2026-09-18T08:00:00+08:00", "J"),
                _flight("MU202", "2026-09-18T08:01:00+08:00", "M"),  # 60 < 180
            ],
        },
    ]
}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_default_matrix_endpoint(client):
    r = client.get("/matrix/default")
    assert r.status_code == 200
    matrix = r.json()["matrix"]
    assert matrix["J"]["M"] == 180
    assert set(matrix) == {"L", "M", "H", "J"}


def test_validate_detects_violations_and_sorts(client):
    r = client.post("/api/v1/validate", json=VALIDATE_BODY)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mode"] == "validate"
    assert data["total_runways"] == 2
    assert data["total_flights"] == 5
    assert data["total_violations"] == 2

    rw36l = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    # stable sort by (time, flight_no)
    assert rw36l["order"] == ["CA101", "CA102", "CA103"]
    assert rw36l["violation_count"] == 1
    v = rw36l["violations"][0]
    assert v["leader"] == "CA101" and v["follower"] == "CA102"
    assert v["required_seconds"] == 120
    assert v["actual_seconds"] == 60
    assert v["shortfall_seconds"] == 60
    assert v["leader_position"] == 1 and v["follower_position"] == 2

    # times emitted as UTC
    assert v["leader_scheduled_off"].endswith("Z") or "+00:00" in v["leader_scheduled_off"]


def test_validate_stable_tiebreak_on_equal_times(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("ZZZ", "2026-09-18T00:00:00Z", "M"),
                    _flight("AAA", "2026-09-18T00:00:00Z", "M"),
                ],
            }
        ]
    }
    data = client.post("/api/v1/validate", json=body).json()
    assert data["runways"][0]["order"] == ["AAA", "ZZZ"]


def test_trial_cascades_and_persists(client):
    r = client.post("/api/v1/trial", json=VALIDATE_BODY)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mode"] == "trial"
    assert data["total_runways"] == 2

    rw36l = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    items = {i["flight_no"]: i for i in rw36l["items"]}
    assert items["CA101"]["delay_seconds"] == 0
    assert items["CA101"]["triggered_by"] is None
    # H->M requires 120s: pushed from 00:01:00 to 00:02:00
    assert items["CA102"]["adjusted_off"] == "2026-09-18T00:02:00Z"
    assert items["CA102"]["delay_seconds"] == 60
    assert items["CA102"]["triggered_by"] == "CA101"
    # cascade: CA103 originally 00:02:10, must be 60s after 00:02:00 -> 00:03:00
    assert items["CA103"]["adjusted_off"] == "2026-09-18T00:03:00Z"
    assert items["CA103"]["delay_seconds"] == 50
    assert items["CA103"]["triggered_by"] == "CA102"
    assert rw36l["delayed_count"] == 2
    assert rw36l["total_delay_seconds"] == 110
    assert rw36l["max_delay_seconds"] == 60

    # 18R: J->M 180s
    rw18r = next(rw for rw in data["runways"] if rw["runway"] == "18R")
    items18 = {i["flight_no"]: i for i in rw18r["items"]}
    assert items18["MU202"]["delay_seconds"] == 120
    assert items18["MU202"]["triggered_by"] == "MU201"
    # +08:00 inputs normalised to UTC
    assert items18["MU201"]["scheduled_off"] == "2026-09-18T00:00:00Z"


def test_batch_roundtrip_validate_and_trial(client):
    v = client.post("/api/v1/validate", json=VALIDATE_BODY).json()
    got = client.get(f"/api/v1/batches/{v['batch_id']}")
    assert got.status_code == 200
    fetched = got.json()
    assert fetched["mode"] == "validate"
    assert fetched["total_violations"] == 2
    assert fetched["runways"][0]["runway"] in {"36L", "18R"}

    t = client.post("/api/v1/trial", json=VALIDATE_BODY).json()
    fetched_t = client.get(f"/api/v1/batches/{t['batch_id']}").json()
    assert fetched_t["mode"] == "trial"
    assert fetched_t["total_delay_seconds"] == 110 + 120

    assert client.get("/api/v1/batches/does-not-exist").status_code == 404


def test_custom_matrix_is_honoured(client):
    body = {
        "runways": [
            {
                "runway": "09",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "H"),
                    _flight("CA2", "2026-09-18T00:00:30Z", "H"),
                ],
            }
        ],
        "matrix": [{"leader": "H", "follower": "H", "seconds": 300}],
    }
    data = client.post("/api/v1/validate", json=body).json()
    # custom matrix only defines H->H
    assert data["matrix"] == {"H": {"H": 300}}
    v = data["runways"][0]["violations"][0]
    assert v["required_seconds"] == 300 and v["shortfall_seconds"] == 270

    trial = client.post("/api/v1/trial", json=body).json()
    item2 = trial["runways"][0]["items"][1]
    assert item2["adjusted_off"] == "2026-09-18T00:05:00Z"
    assert item2["delay_seconds"] == 270


def test_naive_datetime_rejected(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    {"flight_no": "CA1", "scheduled_off": "2026-09-18T00:00:00", "wake": "H"}
                ],
            }
        ]
    }
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 422
    assert "时区" in r.text


def test_duplicate_global_flight_no_rejected(client):
    body = {
        "runways": [
            {"runway": "36L", "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "H")]},
            {"runway": "18R", "flights": [_flight("CA1", "2026-09-18T01:00:00Z", "M")]},
        ]
    }
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 422
    assert "唯一" in r.text


def test_duplicate_runway_and_bad_wake_rejected(client):
    body = {
        "runways": [
            {"runway": "36L", "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "X")]},
        ]
    }
    assert client.post("/api/v1/validate", json=body).status_code == 422

    body2 = {
        "runways": [
            {"runway": "36L", "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "H")]},
            {"runway": "36L", "flights": [_flight("CA2", "2026-09-18T00:01:00Z", "M")]},
        ]
    }
    assert client.post("/api/v1/validate", json=body2).status_code == 422


def test_empty_runways_and_negative_matrix_rejected(client):
    assert client.post("/api/v1/validate", json={"runways": []}).status_code == 422
    body = {
        "runways": [
            {"runway": "36L", "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "H")]},
        ],
        "matrix": [{"leader": "H", "follower": "H", "seconds": -1}],
    }
    assert client.post("/api/v1/validate", json=body).status_code == 422


def test_duplicate_matrix_pair_rejected(client):
    body = {
        "runways": [
            {"runway": "36L", "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "H")]},
        ],
        "matrix": [
            {"leader": "H", "follower": "M", "seconds": 90},
            {"leader": "H", "follower": "M", "seconds": 120},
        ],
    }
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 422
    assert "矩阵组合重复" in r.text
