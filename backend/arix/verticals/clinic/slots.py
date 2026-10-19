"""Slot computation — pure functions, no database, no clock.

This is the most safety-critical maths in the clinic module, so it is written
as total functions over plain dataclasses and covered exhaustively by
``tests/test_slots.py``.  The database layer (``booking.py``) only loads rows
and hands them here.

Rules implemented:

* a slot must fit ``duration_min + buffer_min`` inside a working window;
* breaks split a window in two;
* ``schedule_exceptions`` of type ``off`` remove the day entirely, ``custom``
  replace that day's windows;
* existing visits block their whole period **plus the buffer of the service
  being booked**, because the room/doctor needs that gap;
* slots start on a configurable grid (default 15 min) aligned to the window;
* slots in the past, or closer than ``min_lead_minutes``, are dropped.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from arix.core.timeutil import combine_local, to_tenant

#: Default grid for proposed start times.
DEFAULT_STEP_MIN = 15

PART_OF_DAY_RANGES: dict[str, tuple[time, time]] = {
    "morning": (time(0, 0), time(12, 0)),
    "day": (time(12, 0), time(17, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening": (time(17, 0), time(23, 59, 59)),
}


@dataclass(frozen=True)
class WorkWindow:
    """One continuous working interval on a given local day."""

    start: time
    end: time

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("work window end must be after start")


@dataclass(frozen=True)
class DaySchedule:
    weekday: int
    start: time
    end: time
    break_start: time | None = None
    break_end: time | None = None

    def windows(self) -> list[WorkWindow]:
        if self.break_start and self.break_end and self.start < self.break_start < self.end:
            first = WorkWindow(self.start, self.break_start)
            if self.break_end >= self.end:
                return [first]
            return [first, WorkWindow(self.break_end, self.end)]
        return [WorkWindow(self.start, self.end)]


@dataclass(frozen=True)
class DayException:
    date: date
    type: str  # "off" | "custom"
    start: time | None = None
    end: time | None = None


@dataclass(frozen=True)
class Busy:
    """An already occupied interval, in UTC."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class Slot:
    doctor_id: uuid.UUID
    service_id: uuid.UUID
    start: datetime  # UTC
    end: datetime  # UTC

    def overlaps(self, other: Busy) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass
class DoctorAvailability:
    doctor_id: uuid.UUID
    weekly: list[DaySchedule] = field(default_factory=list)
    exceptions: list[DayException] = field(default_factory=list)
    busy: list[Busy] = field(default_factory=list)

    def windows_for(self, day: date) -> list[WorkWindow]:
        """Resolve the working windows for one local calendar day."""
        day_exceptions = [e for e in self.exceptions if e.date == day]
        if any(e.type == "off" for e in day_exceptions):
            return []
        custom = [e for e in day_exceptions if e.type == "custom" and e.start and e.end]
        if custom:
            return [WorkWindow(e.start, e.end) for e in custom]  # type: ignore[arg-type]
        windows: list[WorkWindow] = []
        for sched in self.weekly:
            if sched.weekday == day.weekday():
                windows.extend(sched.windows())
        return sorted(windows, key=lambda w: w.start)


def _align(value: datetime, step_min: int, origin: datetime) -> datetime:
    """Round ``value`` up to the next ``step_min`` boundary measured from ``origin``."""
    if value <= origin:
        return origin
    delta = (value - origin).total_seconds() / 60
    steps = int(-(-delta // step_min))  # ceil
    return origin + timedelta(minutes=steps * step_min)


def generate_slots(
    availability: DoctorAvailability,
    *,
    service_id: uuid.UUID,
    duration_min: int,
    buffer_min: int = 0,
    date_from: date,
    date_to: date,
    timezone: str,
    now: datetime,
    step_min: int = DEFAULT_STEP_MIN,
    min_lead_minutes: int = 60,
    part_of_day: str | None = None,
    limit: int | None = None,
) -> list[Slot]:
    """Return bookable slots for one doctor, ordered by start time.

    ``now`` is passed in explicitly so the function stays deterministic.
    """
    if duration_min <= 0:
        raise ValueError("duration_min must be positive")
    if date_to < date_from:
        return []

    total_min = duration_min + buffer_min
    earliest = now + timedelta(minutes=max(0, min_lead_minutes))
    window_filter = PART_OF_DAY_RANGES.get((part_of_day or "").lower())

    results: list[Slot] = []
    day = date_from
    while day <= date_to:
        for window in availability.windows_for(day):
            win_start = combine_local(day, window.start, timezone)
            win_end = combine_local(day, window.end, timezone)
            cursor = _align(max(win_start, earliest), step_min, win_start)
            while cursor + timedelta(minutes=total_min) <= win_end:
                slot = Slot(
                    doctor_id=availability.doctor_id,
                    service_id=service_id,
                    start=cursor,
                    end=cursor + timedelta(minutes=duration_min),
                )
                # The doctor must also be free for the trailing buffer.
                occupied_until = cursor + timedelta(minutes=total_min)
                blocked = any(
                    cursor < b.end and b.start < occupied_until for b in availability.busy
                )
                if not blocked and (
                    window_filter is None
                    or window_filter[0]
                    <= to_tenant(slot.start, timezone).time()
                    < window_filter[1]
                ):
                    results.append(slot)
                    if limit and len(results) >= limit:
                        return results
                cursor += timedelta(minutes=step_min)
        day += timedelta(days=1)
    return results


def spread_slots(slots: list[Slot], count: int, timezone: str = "UTC") -> list[Slot]:
    """Pick ``count`` slots that feel like a real choice, not three in a row.

    Buckets by *local* day and part of day, keeps the earliest slot of each
    bucket, then fills up with the earliest remaining ones.  Patients convert
    better on "завтра 10:00 / завтра 14:30 / четверг 11:00" than on three
    consecutive morning slots.
    """
    if count <= 0 or not slots:
        return []
    ordered = sorted(slots, key=lambda s: s.start)
    buckets: dict[tuple[date, int], Slot] = {}
    for slot in ordered:
        local = to_tenant(slot.start, timezone)
        part = 0 if local.hour < 12 else (1 if local.hour < 17 else 2)
        buckets.setdefault((local.date(), part), slot)
    picked = sorted(buckets.values(), key=lambda s: s.start)[:count]
    if len(picked) < count:
        chosen = {s.start for s in picked}
        for slot in ordered:
            if slot.start not in chosen:
                picked.append(slot)
                chosen.add(slot.start)
            if len(picked) >= count:
                break
    return sorted(picked, key=lambda s: s.start)[:count]


def merge_doctor_slots(
    per_doctor: list[list[Slot]], count: int, timezone: str = "UTC"
) -> list[Slot]:
    """Interleave several doctors' slots so one doctor cannot crowd out others."""
    flat: list[Slot] = []
    pools = [sorted(p, key=lambda s: s.start) for p in per_doctor if p]
    index = 0
    while pools and len(flat) < count * 4:
        progressed = False
        for pool in pools:
            if index < len(pool):
                flat.append(pool[index])
                progressed = True
        if not progressed:
            break
        index += 1
    return spread_slots(flat, count, timezone)


__all__ = [
    "DEFAULT_STEP_MIN",
    "Busy",
    "DayException",
    "DaySchedule",
    "DoctorAvailability",
    "Slot",
    "WorkWindow",
    "generate_slots",
    "merge_doctor_slots",
    "spread_slots",
]
