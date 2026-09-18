"""SQLite 连接管理与表结构初始化。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id  TEXT PRIMARY KEY,
    name       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_rules (
    device_id         TEXT PRIMARY KEY REFERENCES devices(device_id),
    temp_min          REAL NOT NULL,
    temp_max          REAL NOT NULL,
    tolerance_seconds REAL NOT NULL,
    merge_gap_seconds REAL NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS temperature_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL REFERENCES devices(device_id),
    ts          TEXT NOT NULL,
    temperature REAL NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(device_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_readings_device_ts
    ON temperature_readings(device_id, ts);
"""


def init_db() -> None:
    """创建数据目录（若需要）并初始化全部表。"""
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
