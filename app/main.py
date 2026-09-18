"""冷链温度事件分析 API。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from . import database
from .analysis import Reading, Rule, detect_events
from .schemas import (
    DeviceIn,
    DeviceOut,
    EventOut,
    EventQueryResult,
    IngestResult,
    ReadingBatchIn,
    ReadingOut,
    RuleIn,
    RuleOut,
    normalize_dt,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="冷链温度事件分析 API",
    description=(
        "接收设备按时间上报的温度读数，依据每台设备的上下限与容忍时长，"
        "识别连续超温事件（短暂恢复不切断、间隔过长分段）。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def to_iso(dt: datetime) -> str:
    """统一存成定宽 UTC 字符串，保证可按字符串比较时间先后。"""
    return normalize_dt(dt).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------- 设备 ----------


@app.put("/api/devices/{device_id}", response_model=DeviceOut)
def upsert_device(device_id: str, body: DeviceIn):
    """注册设备（或更新备注名）。"""
    with database.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO devices(device_id, name, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET name = excluded.name
            """,
            (device_id, body.name, utc_now_iso()),
        )
        row = conn.execute(
            """
            SELECT d.device_id, d.name, d.created_at,
                   (r.device_id IS NOT NULL) AS has_rule
            FROM devices d
            LEFT JOIN device_rules r ON r.device_id = d.device_id
            WHERE d.device_id = ?
            """,
            (device_id,),
        ).fetchone()
    return DeviceOut(
        device_id=row["device_id"],
        name=row["name"],
        created_at=parse_iso(row["created_at"]),
        has_rule=bool(row["has_rule"]),
    )


@app.get("/api/devices", response_model=list[DeviceOut])
def list_devices():
    with database.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.device_id, d.name, d.created_at,
                   (r.device_id IS NOT NULL) AS has_rule
            FROM devices d
            LEFT JOIN device_rules r ON r.device_id = d.device_id
            ORDER BY d.device_id
            """
        ).fetchall()
    return [
        DeviceOut(
            device_id=r["device_id"],
            name=r["name"],
            created_at=parse_iso(r["created_at"]),
            has_rule=bool(r["has_rule"]),
        )
        for r in rows
    ]


# ---------- 设备规则 ----------


@app.put("/api/devices/{device_id}/rule", response_model=RuleOut)
def upsert_rule(device_id: str, body: RuleIn):
    """配置（或更新）设备的温度上下限、容忍时长与容忍缺口。设备不存在时自动注册。"""
    now = utc_now_iso()
    with database.get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO devices(device_id, name, created_at) VALUES (?, NULL, ?)",
            (device_id, now),
        )
        conn.execute(
            """
            INSERT INTO device_rules(
                device_id, temp_min, temp_max, tolerance_seconds,
                merge_gap_seconds, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                temp_min = excluded.temp_min,
                temp_max = excluded.temp_max,
                tolerance_seconds = excluded.tolerance_seconds,
                merge_gap_seconds = excluded.merge_gap_seconds,
                updated_at = excluded.updated_at
            """,
            (
                device_id,
                body.temp_min,
                body.temp_max,
                body.tolerance_seconds,
                body.merge_gap_seconds,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM device_rules WHERE device_id = ?", (device_id,)
        ).fetchone()
    return _row_to_rule(row)


@app.get("/api/devices/{device_id}/rule", response_model=RuleOut)
def get_rule(device_id: str):
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM device_rules WHERE device_id = ?", (device_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="该设备尚未配置温度规则")
    return _row_to_rule(row)


def _row_to_rule(row: sqlite3.Row) -> RuleOut:
    return RuleOut(
        device_id=row["device_id"],
        temp_min=row["temp_min"],
        temp_max=row["temp_max"],
        tolerance_seconds=row["tolerance_seconds"],
        merge_gap_seconds=row["merge_gap_seconds"],
        updated_at=parse_iso(row["updated_at"]),
    )


def _require_device(conn, device_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail="设备不存在，请先注册设备")


def _fetch_rule(conn, device_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM device_rules WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail="该设备尚未配置温度规则，无法分析事件")
    return row


def _row_to_rule_obj(row: sqlite3.Row) -> Rule:
    return Rule(
        temp_min=row["temp_min"],
        temp_max=row["temp_max"],
        tolerance_seconds=row["tolerance_seconds"],
        merge_gap_seconds=row["merge_gap_seconds"],
    )


# ---------- 温度读数 ----------


@app.post("/api/devices/{device_id}/readings", response_model=IngestResult)
def ingest_readings(device_id: str, body: ReadingBatchIn):
    """批量上报温度读数。同一 (device_id, ts) 的重复读数会被忽略。"""
    stored = 0
    duplicates = 0
    with database.get_conn() as conn:
        _require_device(conn, device_id)
        for r in body.readings:
            cur = conn.execute(
                "INSERT OR IGNORE INTO temperature_readings"
                "(device_id, ts, temperature, created_at) VALUES (?, ?, ?, ?)",
                (device_id, to_iso(r.ts), r.temperature, utc_now_iso()),
            )
            if cur.rowcount == 1:
                stored += 1
            else:
                duplicates += 1
    return IngestResult(
        device_id=device_id, stored=stored, duplicates=duplicates
    )


@app.get("/api/devices/{device_id}/readings", response_model=list[ReadingOut])
def list_readings(
    device_id: str,
    start: Optional[datetime] = Query(None, description="起始时间（含），ISO 8601"),
    end: Optional[datetime] = Query(None, description="结束时间（含），ISO 8601"),
    limit: int = Query(1000, ge=1, le=10000),
):
    """按时间范围查询原始读数。"""
    clauses = ["device_id = ?"]
    params: list[object] = [device_id]
    if start is not None:
        clauses.append("ts >= ?")
        params.append(to_iso(normalize_dt(start)))
    if end is not None:
        clauses.append("ts <= ?")
        params.append(to_iso(normalize_dt(end)))
    params.append(limit)
    with database.get_conn() as conn:
        _require_device(conn, device_id)
        rows = conn.execute(
            f"SELECT ts, temperature FROM temperature_readings "
            f"WHERE {' AND '.join(clauses)} ORDER BY ts LIMIT ?",
            params,
        ).fetchall()
    return [
        ReadingOut(ts=parse_iso(r["ts"]), temperature=r["temperature"]) for r in rows
    ]


# ---------- 事件分析 ----------


@app.get("/api/devices/{device_id}/events", response_model=EventQueryResult)
def get_events(
    device_id: str,
    start: Optional[datetime] = Query(None, description="起始时间（含），ISO 8601"),
    end: Optional[datetime] = Query(None, description="结束时间（含），ISO 8601"),
):
    """按时间范围识别并返回连续超温事件。

    事件的识别基于该时间范围内取出的读数（事件若在范围前已开始，
    其起点以范围内首个超温读数为准）。
    """
    clauses = ["device_id = ?"]
    params: list[object] = [device_id]
    if start is not None:
        clauses.append("ts >= ?")
        params.append(to_iso(normalize_dt(start)))
    if end is not None:
        clauses.append("ts <= ?")
        params.append(to_iso(normalize_dt(end)))
    with database.get_conn() as conn:
        _require_device(conn, device_id)
        rule_row = _fetch_rule(conn, device_id)
        rule = _row_to_rule_obj(rule_row)
        rows = conn.execute(
            f"SELECT ts, temperature FROM temperature_readings "
            f"WHERE {' AND '.join(clauses)} ORDER BY ts",
            params,
        ).fetchall()

    readings = [
        Reading(ts=parse_iso(r["ts"]), temperature=r["temperature"]) for r in rows
    ]
    events = detect_events(readings, rule)
    return EventQueryResult(
        device_id=device_id,
        rule=_row_to_rule(rule_row),
        count=len(events),
        events=[
            EventOut(
                index=e.index,
                direction=e.direction,
                start_time=e.start_time,
                end_time=e.end_time,
                peak_temperature=e.peak_temperature,
                peak_time=e.peak_time,
                duration_seconds=e.duration_seconds,
            )
            for e in events
        ],
    )
