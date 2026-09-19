"""Pydantic request/response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .separation import DEFAULT_MATRIX, Wake


class FlightIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flight_no: str = Field(..., min_length=1, examples=["CA1234"])
    scheduled_off: datetime = Field(
        ..., description="计划离场时刻，ISO 8601，必须带时区，例如 2026-09-18T08:00:00+08:00"
    )
    wake: Wake = Field(..., description="尾流等级: L 轻型 / M 中型 / H 重型 / J 超重型")

    @field_validator("flight_no")
    @classmethod
    def _strip_flight_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("flight_no 不能为空")
        return v

    @field_validator("scheduled_off")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("scheduled_off 必须携带时区信息（ISO 8601 offset 或 Z）")
        return v


class RunwayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runway: str = Field(..., min_length=1, examples=["36L"])
    flights: list[FlightIn] = Field(..., min_length=1)

    @field_validator("runway")
    @classmethod
    def _strip_runway(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("runway 不能为空")
        return v

    @model_validator(mode="after")
    def _unique_flight_numbers(self) -> RunwayIn:
        seen: set[str] = set()
        for f in self.flights:
            key = f.flight_no
            if key in seen:
                raise ValueError(f"跑道 {self.runway} 内航班号重复: {key}")
            seen.add(key)
        return self


class MatrixEntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leader: Wake
    follower: Wake
    seconds: int = Field(..., ge=0, description="前机—后机最小雷达间隔（秒）")


class WakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runways: list[RunwayIn] = Field(..., min_length=1)
    matrix: list[MatrixEntryIn] | None = Field(
        default=None,
        description="前机—后机最小间隔矩阵；缺省使用内置 ICAO 风格默认矩阵。"
        "提供时仅列出的组合生效，未列出的组合视为无最小间隔(0 秒)。",
    )

    @model_validator(mode="after")
    def _validate_request(self) -> WakeRequest:
        runway_names: set[str] = set()
        global_flights: set[str] = set()
        for rw in self.runways:
            if rw.runway in runway_names:
                raise ValueError(f"跑道标识重复: {rw.runway}")
            runway_names.add(rw.runway)
            for f in rw.flights:
                if f.flight_no in global_flights:
                    raise ValueError(f"航班号在整个请求中必须唯一: {f.flight_no}")
                global_flights.add(f.flight_no)
        if self.matrix is not None:
            pairs: set[tuple[str, str]] = set()
            for entry in self.matrix:
                pair = (entry.leader.value, entry.follower.value)
                if pair in pairs:
                    raise ValueError(
                        f"间隔矩阵组合重复: leader={pair[0]} follower={pair[1]}"
                    )
                pairs.add(pair)
        return self


# ---------- response models ----------


class ViolationOut(BaseModel):
    leader: str
    follower: str
    leader_wake: Wake
    follower_wake: Wake
    leader_scheduled_off: datetime
    follower_scheduled_off: datetime
    required_seconds: int
    actual_seconds: int
    shortfall_seconds: int
    leader_position: int
    follower_position: int


class RunwayReportOut(BaseModel):
    runway: str
    order: list[str]
    total: int
    violation_count: int
    violations: list[ViolationOut]


class ValidateResponse(BaseModel):
    batch_id: str
    mode: str = "validate"
    matrix: dict[str, dict[str, int]]
    total_runways: int
    total_flights: int
    total_violations: int
    runways: list[RunwayReportOut]


class TrialItemOut(BaseModel):
    position: int
    flight_no: str
    wake: Wake
    scheduled_off: datetime
    adjusted_off: datetime
    delay_seconds: int
    triggered_by: str | None


class RunwayTrialOut(BaseModel):
    runway: str
    order: list[str]
    total: int
    delayed_count: int
    total_delay_seconds: int
    max_delay_seconds: int
    items: list[TrialItemOut]


class TrialResponse(BaseModel):
    batch_id: str
    mode: str = "trial"
    matrix: dict[str, dict[str, int]]
    total_runways: int
    total_flights: int
    total_delayed: int
    total_delay_seconds: int
    runways: list[RunwayTrialOut]


class MatrixOut(BaseModel):
    matrix: dict[str, dict[str, int]]


class HealthOut(BaseModel):
    status: str


class BatchResponse(BaseModel):
    """Stored batch (either mode) fetched by id; summary fields are mode-specific."""

    batch_id: str
    mode: str
    created_at: datetime
    matrix: dict[str, dict[str, int]]
    total_runways: int
    total_flights: int
    total_violations: int | None = None
    total_delayed: int | None = None
    total_delay_seconds: int | None = None
    runways: list[dict]


def matrix_to_nested(flat: dict[tuple[str, str], int]) -> dict[str, dict[str, int]]:
    """Convert the internal flat matrix to a leader->follower nested dict."""
    nested: dict[str, dict[str, int]] = {}
    for (leader, follower), seconds in flat.items():
        nested.setdefault(leader, {})[follower] = seconds
    return nested


def default_matrix_nested() -> dict[str, dict[str, int]]:
    return {k: dict(v) for k, v in DEFAULT_MATRIX.items()}
