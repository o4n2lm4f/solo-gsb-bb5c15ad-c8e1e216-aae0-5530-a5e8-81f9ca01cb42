"""请求 / 响应数据模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


def normalize_dt(dt: datetime) -> datetime:
    """naive 时间视为 UTC；带时区的时间统一换算到 UTC。"""
    from datetime import timezone

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class RuleIn(BaseModel):
    temp_min: float = Field(..., description="温度下限（含），摄氏度")
    temp_max: float = Field(..., description="温度上限（含），摄氏度")
    tolerance_seconds: float = Field(
        ..., gt=0, description="容忍时长（秒）：累计超限达到该值才记为一起事件"
    )
    merge_gap_seconds: float = Field(
        ...,
        gt=0,
        description="容忍缺口（秒）：短暂恢复/上报间隔不超过该值则不切断事件",
    )

    @model_validator(mode="after")
    def _check_range(self):
        if self.temp_min >= self.temp_max:
            raise ValueError("temp_min 必须小于 temp_max")
        return self


class RuleOut(RuleIn):
    device_id: str
    updated_at: datetime


class DeviceIn(BaseModel):
    name: Optional[str] = Field(None, description="设备备注名")


class DeviceOut(BaseModel):
    device_id: str
    name: Optional[str] = None
    created_at: datetime
    has_rule: bool = False


class ReadingIn(BaseModel):
    ts: datetime = Field(..., description="读数时间，ISO 8601；不带时区时按 UTC 处理")
    temperature: float = Field(..., description="温度读数，摄氏度")

    @field_validator("temperature")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("temperature 必须是有限数值")
        return v

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return normalize_dt(v)


class ReadingBatchIn(BaseModel):
    readings: list[ReadingIn] = Field(..., min_length=1)


class ReadingOut(BaseModel):
    ts: datetime
    temperature: float


class IngestResult(BaseModel):
    device_id: str
    stored: int
    duplicates: int


class EventOut(BaseModel):
    index: int
    direction: Literal["high", "low"]
    start_time: datetime
    end_time: datetime
    peak_temperature: float
    peak_time: datetime
    duration_seconds: float


class EventQueryResult(BaseModel):
    device_id: str
    rule: RuleOut
    count: int
    events: list[EventOut]
