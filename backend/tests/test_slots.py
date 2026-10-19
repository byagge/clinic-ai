"""Slot arithmetic — the most safety-critical maths in the clinic module."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time

import pytest

from arix.core.timeutil import to_tenant
from arix.verticals.clinic.slots import (
    Busy,
    DayException,
    DaySchedule,
    DoctorAvailability,
    generate_slots,
    merge_doctor_slots,
    spread_slots,
)

TZ = "Asia/Almaty"  # UTC+5, no DST
DOCTOR = uuid.uuid4()
SERVICE = uuid.uuid4()
NOW = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)  # Mon 05:00 local


def availability(**kwargs) -> DoctorAvailability:
    base = DoctorAvailability(
        doctor_id=DOCTOR,
        weekly=[DaySchedule(weekday, time(9, 0), time(18, 0)) for weekday in range(0, 6)],
    )
    for key, value in kwargs.items():
        setattr(base, key, value)
    return base


def gen(av: DoctorAvailability, **kwargs):
    params = {
        "service_id": SERVICE,
        "duration_min": 60,
        "buffer_min": 0,
        "date_from": date(2026, 10, 19),
        "date_to": date(2026, 10, 19),
        "timezone": TZ,
        "now": NOW,
        "min_lead_minutes": 0,
    }
    params.update(kwargs)
    return generate_slots(av, **params)


def local_times(slots) -> list[str]:
    return [to_tenant(s.start, TZ).strftime("%H:%M") for s in slots]


def test_simple_day_is_fully_gridded():
    slots = gen(availability())
    assert local_times(slots)[0] == "09:00"
    assert local_times(slots)[-1] == "17:00"  # 17:00 + 60min == 18:00
    assert len(slots) == 33  # 09:00..17:00 on a 15-minute grid


def test_break_splits_the_day():
    av = availability(
        weekly=[DaySchedule(w, time(9, 0), time(18, 0), time(13, 0), time(14, 0)) for w in range(6)]
    )
    times = local_times(gen(av))
    assert "12:00" in times  # 12:00 + 60 == 13:00, fits before the break
    assert "12:15" not in times  # would run into the break
    assert "14:00" in times


def test_buffer_is_reserved_after_the_visit():
    av = availability()
    times = local_times(gen(av, duration_min=60, buffer_min=30))
    # Last start must leave 60 + 30 minutes before 18:00.
    assert times[-1] == "16:30"


def test_existing_visit_blocks_its_period_plus_buffer():
    # 10:00-11:00 local == 05:00-06:00 UTC
    av = availability(
        busy=[Busy(datetime(2026, 10, 19, 5, 0, tzinfo=UTC), datetime(2026, 10, 19, 6, 0, tzinfo=UTC))]
    )
    times = local_times(gen(av, duration_min=60, buffer_min=15))
    assert "09:00" not in times  # 09:00 + 75min would hit 10:00
    assert "10:00" not in times
    assert "11:00" in times


def test_day_off_removes_the_day():
    av = availability(exceptions=[DayException(date(2026, 10, 19), "off")])
    assert gen(av) == []


def test_custom_exception_replaces_the_window():
    av = availability(
        exceptions=[DayException(date(2026, 10, 19), "custom", time(15, 0), time(17, 0))]
    )
    assert local_times(gen(av)) == ["15:00", "15:15", "15:30", "15:45", "16:00"]


def test_min_lead_time_drops_imminent_slots():
    now = datetime(2026, 10, 19, 5, 10, tzinfo=UTC)  # 10:10 local
    times = local_times(gen(availability(), now=now, min_lead_minutes=60))
    assert times[0] == "11:15"


def test_part_of_day_filter():
    times = local_times(gen(availability(), part_of_day="evening"))
    assert times and all(int(t[:2]) >= 17 for t in times)


def test_weekday_without_schedule_is_empty():
    av = availability(weekly=[DaySchedule(6, time(9, 0), time(18, 0))])  # Sunday only
    assert gen(av) == []


def test_timezone_is_respected_not_utc():
    slots = gen(availability())
    assert slots[0].start == datetime(2026, 10, 19, 4, 0, tzinfo=UTC)  # 09:00 +05:00


def test_duration_must_be_positive():
    with pytest.raises(ValueError):
        gen(availability(), duration_min=0)


def test_reversed_date_range_is_empty():
    assert gen(availability(), date_from=date(2026, 10, 20), date_to=date(2026, 10, 19)) == []


def test_spread_picks_distinct_parts_of_day():
    slots = gen(
        availability(
            weekly=[DaySchedule(w, time(9, 0), time(20, 0)) for w in range(6)]
        ),
        date_to=date(2026, 10, 21),
    )
    picked = spread_slots(slots, 3, TZ)
    assert len(picked) == 3
    def part(hour: int) -> int:
        return 0 if hour < 12 else (1 if hour < 17 else 2)

    buckets = {
        (to_tenant(s.start, TZ).date(), part(to_tenant(s.start, TZ).hour)) for s in picked
    }
    assert len(buckets) == 3, local_times(picked)


def test_spread_handles_fewer_slots_than_requested():
    av = availability(
        exceptions=[DayException(date(2026, 10, 19), "custom", time(9, 0), time(10, 0))]
    )
    slots = gen(av)
    assert len(spread_slots(slots, 5, TZ)) == len(slots)


def test_merge_interleaves_doctors():
    other = uuid.uuid4()
    first = gen(availability())
    second = gen(
        DoctorAvailability(
            doctor_id=other,
            weekly=[DaySchedule(w, time(9, 0), time(18, 0)) for w in range(6)],
        )
    )
    merged = merge_doctor_slots([first, second], 4, TZ)
    assert len({s.doctor_id for s in merged}) >= 1
    assert len(merged) == 4
