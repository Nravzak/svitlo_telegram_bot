import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from config import API_URL, CACHE_TTL_SECONDS, DEFAULT_REGION, TZ


HALF_HOUR = timedelta(minutes=30)


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime  # end is exclusive


# -------- In-memory cache (raw API per region) --------
# key: region_cpu -> (expires_epoch, raw_json)
_RAW_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _date_to_dt(date_str: str) -> datetime:
    # 2026-02-11 -> 2026-02-11 00:00 in TZ
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.replace(tzinfo=TZ)


def _time_str_from_dt(dt: datetime) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}"


def _round_down_to_half_hour(dt: datetime) -> datetime:
    minute = 0 if dt.minute < 30 else 30
    return dt.replace(minute=minute, second=0, microsecond=0)


def _iter_half_hours(day_start: datetime) -> List[datetime]:
    # returns list of datetimes: 00:00 .. 23:30
    times: List[datetime] = []
    cur = day_start
    end = day_start + timedelta(days=1)
    while cur < end:
        times.append(cur)
        cur += HALF_HOUR
    return times


def _slots_normalized_for_hash(date_str: str, slots: Dict[str, int]) -> str:
    day_start = _date_to_dt(date_str)
    parts: List[str] = []
    for t in _iter_half_hours(day_start):
        key = _time_str_from_dt(t)
        status = int(slots.get(key, 1))  # если ключа нет — считаем "свет есть"
        parts.append(f"{date_str} {key}={status}")
    return "|".join(parts)


def _hash_schedule(date_today: str, slots_today: Dict[str, int],
                   date_tomorrow: str, slots_tomorrow: Dict[str, int]) -> str:
    s = _slots_normalized_for_hash(date_today, slots_today) + "||" + _slots_normalized_for_hash(date_tomorrow, slots_tomorrow)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _slots_to_off_intervals(date_str: str, slots: Dict[str, int]) -> List[Interval]:
    """
    slots: {"00:00":1, "00:30":2, ...}
    status: 1=power on, 2=power off
    Convert consecutive 2-slots into intervals.
    """
    day_start = _date_to_dt(date_str)
    times = _iter_half_hours(day_start)

    intervals: List[Interval] = []
    in_off = False
    off_start: Optional[datetime] = None

    for i, t in enumerate(times):
        key = _time_str_from_dt(t)
        status = int(slots.get(key, 1))  # missing -> assume power on
        is_off = (status == 2)

        if is_off and not in_off:
            in_off = True
            off_start = t

        if (not is_off) and in_off:
            # close interval at current time
            intervals.append(Interval(start=off_start, end=t))  # type: ignore[arg-type]
            in_off = False
            off_start = None

        # last slot edge: if day ends while still off
        if i == len(times) - 1 and in_off:
            intervals.append(Interval(start=off_start, end=day_start + timedelta(days=1)))  # type: ignore[arg-type]

    return intervals


def _total_minutes(intervals: List[Interval]) -> int:
    total = 0
    for it in intervals:
        total += int((it.end - it.start).total_seconds() // 60)
    return total


def _find_next_outage(now: datetime, today_off: List[Interval], tomorrow_off: List[Interval]) -> Optional[datetime]:
    # If now is inside an outage interval, next outage is the next interval after now (not the current one)
    all_intervals = sorted(today_off + tomorrow_off, key=lambda x: x.start)

    for it in all_intervals:
        if it.start > now:
            return it.start
    return None


def _is_now_has_power(now: datetime, date_today: str, slots_today: Dict[str, int]) -> bool:
    # Determine by slot that covers "now" rounded down to 30 minutes
    now_rounded = _round_down_to_half_hour(now)
    today_start = _date_to_dt(date_today)
    if not (today_start <= now_rounded < today_start + timedelta(days=1)):
        # if day mismatch, assume power on (safe fallback)
        return True

    key = _time_str_from_dt(now_rounded)
    status = int(slots_today.get(key, 1))
    return status == 1


async def _fetch_raw(region_cpu: str) -> Dict[str, Any]:
    # cache
    now = time.time()
    cached = _RAW_CACHE.get(region_cpu)
    if cached and cached[0] > now:
        return cached[1]

    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(API_URL) as resp:
            resp.raise_for_status()
            data = await resp.json()

    _RAW_CACHE[region_cpu] = (now + CACHE_TTL_SECONDS, data)
    return data


def _extract_region_block(payload: Dict[str, Any], region_cpu: str) -> Dict[str, Any]:
    regions = payload.get("regions", [])
    for r in regions:
        if r.get("cpu") == region_cpu:
            return r
    raise ValueError(f"Region '{region_cpu}' not found in API response")


def _extract_slots(region_block: Dict[str, Any], group_name: str, date_str: str) -> Dict[str, int]:
    """
    region_block["schedule"][group][date] -> { "00:00": 1/2, ... }
    """
    schedule = region_block.get("schedule", {}) or {}
    group_block = schedule.get(group_name, {}) or {}
    day_slots = group_block.get(date_str, {}) or {}

    # ensure ints
    normalized: Dict[str, int] = {}
    for k, v in day_slots.items():
        try:
            normalized[k] = int(v)
        except Exception:
            # skip weird values
            continue
    return normalized


async def get_schedule(group_name: str, region_cpu: str = DEFAULT_REGION) -> Dict[str, Any]:
    """
    Returns normalized schedule data for one region+group:
    - now_has_power
    - today_off intervals
    - tomorrow_off intervals
    - total off today minutes
    - next outage start + minutes
    - schedule_hash
    """
    payload = await _fetch_raw(region_cpu)

    date_today = str(payload.get("date_today"))
    date_tomorrow = str(payload.get("date_tomorrow"))

    region_block = _extract_region_block(payload, region_cpu)

    slots_today = _extract_slots(region_block, group_name, date_today)
    slots_tomorrow = _extract_slots(region_block, group_name, date_tomorrow)

    today_off = _slots_to_off_intervals(date_today, slots_today)
    tomorrow_off = _slots_to_off_intervals(date_tomorrow, slots_tomorrow)

    now = datetime.now(TZ)

    now_has_power = _is_now_has_power(now, date_today, slots_today)

    next_outage_start = _find_next_outage(now, today_off, tomorrow_off)
    next_outage_in_minutes: Optional[int] = None
    if next_outage_start:
        delta_min = int(((next_outage_start - now).total_seconds() + 59) // 60)  # ceil minutes
        next_outage_in_minutes = max(delta_min, 0)

    schedule_hash = _hash_schedule(date_today, slots_today, date_tomorrow, slots_tomorrow)

    return {
        "region": region_cpu,
        "group": group_name,
        "date_today": date_today,
        "date_tomorrow": date_tomorrow,
        "now_has_power": now_has_power,
        "today_off": today_off,
        "tomorrow_off": tomorrow_off,
        "total_off_today_minutes": _total_minutes(today_off),
        "next_outage_start_dt": next_outage_start,
        "next_outage_in_minutes": next_outage_in_minutes,
        "schedule_hash": schedule_hash,
        "region_name_ua": region_block.get("name_ua"),
    }


# ---- quick local test ----
if __name__ == "__main__":
    async def _t():
        data = await get_schedule("2.1")
        print("region:", data["region"], data.get("region_name_ua"))
        print("group:", data["group"])
        print("date_today:", data["date_today"])
        print("now_has_power:", data["now_has_power"])
        print("today_off_minutes:", data["total_off_today_minutes"])
        print("today_off:")
        for it in data["today_off"]:
            print(" ", it.start.strftime("%H:%M"), "-", it.end.strftime("%H:%M"))
        print("next_outage_in_minutes:", data["next_outage_in_minutes"])
        print("hash:", data["schedule_hash"][:12], "...")

    asyncio.run(_t())
