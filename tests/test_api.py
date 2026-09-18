"""API 集成测试：每个测试会话使用独立的临时 SQLite 文件。"""
import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["COLDCHAIN_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


DEVICE = "truck-001"


def _rule_payload():
    return {
        "temp_min": 2.0,
        "temp_max": 8.0,
        "tolerance_seconds": 60,
        "merge_gap_seconds": 120,
    }


def test_full_flow(client):
    # 1. 注册设备
    r = client.put(f"/api/devices/{DEVICE}", json={"name": "沪A-冷链车1号"})
    assert r.status_code == 200
    assert r.json()["has_rule"] is False

    # 2. 配置规则（自动补注册设备的路径同样可用，这里设备已存在）
    r = client.put(f"/api/devices/{DEVICE}/rule", json=_rule_payload())
    assert r.status_code == 200
    assert r.json()["device_id"] == DEVICE

    r = client.get(f"/api/devices/{DEVICE}/rule")
    assert r.status_code == 200
    assert r.json()["temp_max"] == 8.0

    # 3. 上报读数：中间一次短暂恢复，应合并为一起事件
    readings = [
        {"ts": "2026-09-18T08:00:00Z", "temperature": 9.0},
        {"ts": "2026-09-18T08:01:00Z", "temperature": 10.0},
        {"ts": "2026-09-18T08:02:00Z", "temperature": 5.0},
        {"ts": "2026-09-18T08:03:00Z", "temperature": 9.0},
    ]
    r = client.post(f"/api/devices/{DEVICE}/readings", json={"readings": readings})
    assert r.status_code == 200
    assert r.json()["stored"] == 4
    assert r.json()["duplicates"] == 0

    # 重复上报应被忽略
    r = client.post(f"/api/devices/{DEVICE}/readings", json={"readings": readings})
    assert r.json()["stored"] == 0
    assert r.json()["duplicates"] == 4

    # 4. 按时间范围查询原始读数
    r = client.get(
        f"/api/devices/{DEVICE}/readings",
        params={"start": "2026-09-18T08:02:00Z"},
    )
    assert r.status_code == 200
    assert [row["temperature"] for row in r.json()] == [5.0, 9.0]

    # 5. 查询事件：短暂恢复不切断
    r = client.get(f"/api/devices/{DEVICE}/events")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    e = body["events"][0]
    assert e["direction"] == "high"
    assert e["start_time"].startswith("2026-09-18T08:00:00")
    assert e["end_time"].startswith("2026-09-18T08:03:00")
    assert e["peak_temperature"] == 10.0
    assert e["duration_seconds"] == 120.0

    # 6. 时间范围限定后，只剩一个孤立超温读数，累计 0 秒，不产生事件
    r = client.get(
        f"/api/devices/{DEVICE}/events",
        params={"start": "2026-09-18T08:02:30Z"},
    )
    assert r.json()["count"] == 0

    # 7. 设备列表
    r = client.get("/api/devices")
    assert any(d["device_id"] == DEVICE and d["has_rule"] for d in r.json())


def test_errors(client):
    # 无效规则：下限 >= 上限
    client.put("/api/devices/bad-rule", json={})
    r = client.put(
        "/api/devices/bad-rule/rule",
        json={**_rule_payload(), "temp_min": 9, "temp_max": 1},
    )
    assert r.status_code == 422

    # 未配置规则的设备查询事件 → 409
    client.put("/api/devices/no-rule", json={})
    r = client.get("/api/devices/no-rule/events")
    assert r.status_code == 409

    # 未注册设备上报读数 → 409
    r = client.post(
        "/api/devices/ghost/readings",
        json={"readings": [{"ts": "2026-09-18T08:00:00Z", "temperature": 9}]},
    )
    assert r.status_code == 409

    # 缺失规则 → 404
    assert client.get("/api/devices/ghost/rule").status_code == 404

    # 非有限温度 → 422
    r = client.post(
        f"/api/devices/{DEVICE}/readings",
        json={"readings": [{"ts": "2026-09-18T09:00:00Z", "temperature": "nan"}]},
    )
    assert r.status_code == 422


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
