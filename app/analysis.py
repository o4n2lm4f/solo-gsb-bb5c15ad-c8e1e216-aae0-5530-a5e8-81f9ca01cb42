"""连续超温事件识别算法（纯函数，无数据库依赖，便于单测）。

判定约定（采样保持口径）
------------------------
设备按时间上报读数，相邻两次读数之间的区间，温度状态归属于前一条读数：
即第 i 条读数的温度状态持续到第 i+1 条读数到来为止；序列最后一条读数之后
没有持续时长（没有下一次上报来界定）。

一条读数的状态：
- ``high``：temperature > temp_max
- ``low``： temperature < temp_min
- ``ok``：  temp_min <= temperature <= temp_max

事件合并 / 切断规则
-------------------
1. **短暂恢复不切断**：超温过程中出现 ``ok`` 读数，若从上一个超温读数到下一个
   超温读数的时间间隔不超过 ``merge_gap_seconds``（容忍缺口），则继续视为
   同一起事件，恢复区间不计入累计超限时长。
2. **间隔过长则分段**：超过 ``merge_gap_seconds`` 没有再次出现超温读数，
   当前事件在缺口处结束（恢复区间 / 缺失区间同样不计入超限时长）。
3. 高、低温方向不同（high↔low）同样按上面的缺口规则判断是否为一起事件，
   事件的峰值按偏离 [min, max] 最严重的那次读数记录，并标注方向。

事件起止与累计时长
------------------
- ``start_time``：事件中第一个超温读数的时间；
- ``end_time``：缺口导致结束时为“最后一次落在容忍缺口内的读数（含恢复读数）”
  的时间，序列自然结束时为最后一个超温读数的时间；
- ``duration_seconds``：事件区间内所有“超温状态区间”的长度之和，
  恢复区间不计入；
- 累计超限时长不足 ``tolerance_seconds`` 的事件不输出。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal, Optional

ViolationType = Literal["high", "low"]


@dataclass(frozen=True)
class Reading:
    ts: datetime
    temperature: float


@dataclass(frozen=True)
class Rule:
    temp_min: float
    temp_max: float
    tolerance_seconds: float
    merge_gap_seconds: float


@dataclass(frozen=True)
class Event:
    index: int
    direction: ViolationType
    start_time: datetime
    end_time: datetime
    peak_temperature: float
    peak_time: datetime
    duration_seconds: float


def _violation(temperature: float, rule: Rule) -> Optional[ViolationType]:
    if temperature > rule.temp_max:
        return "high"
    if temperature < rule.temp_min:
        return "low"
    return None


def _deviation(temperature: float, rule: Rule) -> float:
    """读数偏离正常区间 [min, max] 的幅度（正常读数为 0）。"""
    if temperature > rule.temp_max:
        return temperature - rule.temp_max
    if temperature < rule.temp_min:
        return rule.temp_min - temperature
    return 0.0


def detect_events(
    readings: Iterable[Reading],
    rule: Rule,
) -> list[Event]:
    """识别连续超温事件。

    输入读数无需排序，函数内部会按时间升序处理。
    """
    ordered = sorted(readings, key=lambda r: r.ts)
    events: list[Event] = []

    # 正在进行中的事件状态
    ev_direction: Optional[ViolationType] = None
    ev_start: Optional[datetime] = None
    ev_end: Optional[datetime] = None           # 暂定结束时间（可能随恢复读数延后）
    ev_last_violation_ts: Optional[datetime] = None
    ev_peak_temp: Optional[float] = None
    ev_peak_ts: Optional[datetime] = None
    ev_duration = 0.0

    prev_ts: Optional[datetime] = None
    prev_status: Optional[ViolationType | Literal["ok"]] = None

    def close_event() -> None:
        nonlocal ev_direction, ev_start, ev_end, ev_last_violation_ts
        nonlocal ev_peak_temp, ev_peak_ts, ev_duration
        assert ev_direction is not None
        assert ev_start is not None
        assert ev_end is not None
        assert ev_peak_temp is not None
        assert ev_peak_ts is not None
        if ev_duration >= rule.tolerance_seconds:
            events.append(
                Event(
                    index=len(events) + 1,
                    direction=ev_direction,
                    start_time=ev_start,
                    end_time=ev_end,
                    peak_temperature=ev_peak_temp,
                    peak_time=ev_peak_ts,
                    duration_seconds=round(ev_duration, 3),
                )
            )
        ev_direction = None
        ev_start = None
        ev_end = None
        ev_last_violation_ts = None
        ev_peak_temp = None
        ev_peak_ts = None
        ev_duration = 0.0

    def start_event(direction: ViolationType, reading: Reading) -> None:
        nonlocal ev_direction, ev_start, ev_end, ev_last_violation_ts
        nonlocal ev_peak_temp, ev_peak_ts
        ev_direction = direction
        ev_start = reading.ts
        ev_end = reading.ts
        ev_last_violation_ts = reading.ts
        ev_peak_temp = reading.temperature
        ev_peak_ts = reading.ts

    for cur in ordered:
        status: Optional[ViolationType | Literal["ok"]] = _violation(
            cur.temperature, rule
        )  # None 表示 ok（放在正常区间内）
        status = status if status is not None else "ok"

        if prev_ts is not None:
            gap = (cur.ts - prev_ts).total_seconds()

            if ev_direction is not None:
                gap_from_violation = (
                    cur.ts - ev_last_violation_ts  # type: ignore[operator]
                ).total_seconds()

                if gap_from_violation > rule.merge_gap_seconds:
                    # 缺口过长：上一起事件在缺口处结束，当前区间不归属任何事件
                    close_event()
                else:
                    if prev_status in ("high", "low"):
                        # 上一读数超温，本区间归属于它（短暂恢复区间由此自然排除）
                        ev_duration += gap
                    # prev_status == "ok"：恢复区间，不计入超限时长
                    ev_end = cur.ts

        if status in ("high", "low"):
            if ev_direction is None:
                start_event(status, cur)  # type: ignore[arg-type]
            else:
                ev_last_violation_ts = cur.ts
                ev_end = cur.ts
                if _deviation(cur.temperature, rule) > _deviation(
                    ev_peak_temp, rule  # type: ignore[arg-type]
                ):
                    ev_peak_temp = cur.temperature
                    ev_peak_ts = cur.ts
        # status == "ok"：仅用于在上面的逻辑中延后暂定结束时间，不更新峰值

        prev_ts = cur.ts
        prev_status = status

    if ev_direction is not None:
        close_event()

    return events
