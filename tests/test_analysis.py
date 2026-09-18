"""识别算法单元测试。"""
from datetime import datetime, timedelta, timezone

from app.analysis import Event, Reading, Rule, detect_events

RULE = Rule(
    temp_min=2.0,
    temp_max=8.0,
    tolerance_seconds=60.0,
    merge_gap_seconds=120.0,
)


def t(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 18, 8, minute, second, tzinfo=timezone.utc)


def test_no_violation():
    readings = [Reading(t(0), 5.0), Reading(t(1), 6.0)]
    assert detect_events(readings, RULE) == []


def test_single_violation_below_tolerance():
    # 只有一个超温读数，没有后续区间，累计时长为 0，被容忍时长过滤
    events = detect_events([Reading(t(0), 9.0)], RULE)
    assert events == []


def test_simple_high_event():
    readings = [Reading(t(0), 9.0), Reading(t(1), 10.0), Reading(t(2), 9.0)]
    events = detect_events(readings, RULE)
    assert len(events) == 1
    e = events[0]
    assert e.direction == "high"
    assert e.start_time == t(0)
    assert e.end_time == t(2)
    assert e.peak_temperature == 10.0
    assert e.peak_time == t(1)
    # 08:00→08:02 共两个区间，每个 60 秒
    assert e.duration_seconds == 120.0


def test_low_direction_and_most_severe_peak():
    readings = [Reading(t(0), 1.0), Reading(t(1), 0.0), Reading(t(2), 1.0)]
    events = detect_events(readings, RULE)
    assert len(events) == 1
    e = events[0]
    assert e.direction == "low"
    # 偏离下限最严重的是 0.0（偏离 2 度）
    assert e.peak_temperature == 0.0
    assert e.peak_time == t(1)
    assert e.duration_seconds == 120.0


def test_brief_recovery_does_not_split():
    """恢复读数落在容忍缺口内：事件不切断，恢复区间不计入超限时长。"""
    readings = [
        Reading(t(0), 9.0),
        Reading(t(1), 10.0),
        Reading(t(2), 5.0),   # 短暂恢复
        Reading(t(3), 9.0),
    ]
    events = detect_events(readings, RULE)
    assert len(events) == 1
    e = events[0]
    assert e.start_time == t(0)
    assert e.end_time == t(3)
    assert e.peak_temperature == 10.0
    # 08:01→08:02 与 08:02→08:03 中，只有恢复前两个超温区间计入
    assert e.duration_seconds == 120.0


def test_recovery_exactly_at_gap_boundary_merges():
    """间隔恰好等于容忍缺口：属于“未超过”，不切断。"""
    readings = [
        Reading(t(0), 9.0),
        Reading(t(1), 9.0),
        Reading(t(3), 5.0),   # 距上次超温 120 秒，恰好等于边界
        Reading(t(4), 9.0),
    ]
    events = detect_events(readings, RULE)
    assert len(events) == 1
    assert events[0].start_time == t(0)


def test_long_gap_splits_event():
    """间隔超过容忍缺口：分段为两起事件。"""
    readings = [
        Reading(t(0), 9.0),
        Reading(t(1), 9.0),
        Reading(t(4), 5.0),   # 距上次超温 180 秒 > 120，切断
        Reading(t(5), 9.0),
        Reading(t(6), 9.0),
    ]
    events = detect_events(readings, RULE)
    assert len(events) == 2
    first, second = events
    assert first.index == 1
    assert first.start_time == t(0)
    assert first.end_time == t(1)       # 在缺口处结束
    assert first.duration_seconds == 60.0
    assert second.index == 2
    assert second.start_time == t(5)
    assert second.end_time == t(6)
    assert second.duration_seconds == 60.0


def test_unsorted_input_is_handled():
    readings = [
        Reading(t(2), 9.0),
        Reading(t(0), 9.0),
        Reading(t(1), 10.0),
    ]
    events = detect_events(readings, RULE)
    assert len(events) == 1
    assert events[0].start_time == t(0)
    assert events[0].peak_temperature == 10.0


def test_direction_switch_within_gap_keeps_single_event():
    """缺口内高温转低温：按文档约定仍为一起事件，峰值取偏离最严重者。"""
    readings = [Reading(t(0), 9.0), Reading(t(1), 0.0)]
    events = detect_events(readings, RULE)
    assert len(events) == 1
    e = events[0]
    assert e.peak_temperature == 0.0  # 偏离 2 度 > 高温的 1 度
    assert e.duration_seconds == 60.0


def test_event_index_is_sequential():
    readings = [
        Reading(t(0), 9.0),
        Reading(t(1), 9.0),
        Reading(t(4), 5.0),
        Reading(t(5), 9.0),
        Reading(t(6), 9.0),
    ]
    events = detect_events(readings, RULE)
    assert [e.index for e in events] == [1, 2]
    assert all(isinstance(e, Event) for e in events)


def test_recovery_just_past_boundary_splits():
    """恢复读数本身超出缺口：立即切断。"""
    readings = [
        Reading(t(0), 9.0),
        Reading(t(1), 9.0),
        Reading(t(2, 30), 9.0),  # 90s gap, still merged
        Reading(t(5), 5.0),      # 距上次超温 150s > 120
        Reading(t(6), 9.0),
        Reading(t(7), 9.0),
    ]
    events = detect_events(readings, RULE)
    assert len(events) == 2
    assert events[0].duration_seconds == 150.0
    assert events[1].duration_seconds == 60.0
