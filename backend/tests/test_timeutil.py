"""Quiet hours and timezone helpers."""
from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from arix.core.timeutil import (
    QuietHours,
    combine_local,
    local_date,
    month_period,
    parse_hhmm,
    shift_out_of_quiet_hours,
    to_tenant,
    to_utc,
)

TZ = "Asia/Almaty"


def test_parse_quiet_hours_variants():
    assert QuietHours.parse("21:00-09:00") == QuietHours(time(21), time(9))
    assert QuietHours.parse("21:00—09:00") == QuietHours(time(21), time(9))
    assert QuietHours.parse(None) is None
    assert QuietHours.parse("nonsense") is None


def test_wrapping_window_contains_night_hours():
    quiet = QuietHours.parse("21:00-09:00")
    assert quiet.wraps_midnight
    for hour in (21, 23, 0, 3, 8):
        assert quiet.contains_local(datetime(2026, 10, 19, hour, 30))
    for hour in (9, 12, 20):
        assert not quiet.contains_local(datetime(2026, 10, 19, hour, 30))


def test_non_wrapping_window():
    quiet = QuietHours.parse("13:00-14:00")
    assert not quiet.wraps_midnight
    assert quiet.contains_local(datetime(2026, 10, 19, 13, 30))
    assert not quiet.contains_local(datetime(2026, 10, 19, 14, 0))


def test_next_allowed_moves_to_window_end():
    quiet = QuietHours.parse("21:00-09:00")
    at_22 = datetime(2026, 10, 19, 22, 0)
    assert quiet.next_allowed_local(at_22) == datetime(2026, 10, 20, 9, 0)
    at_3 = datetime(2026, 10, 20, 3, 0)
    assert quiet.next_allowed_local(at_3) == datetime(2026, 10, 20, 9, 0)
    at_noon = datetime(2026, 10, 19, 12, 0)
    assert quiet.next_allowed_local(at_noon) == at_noon


def test_shift_out_of_quiet_hours_uses_tenant_timezone():
    quiet = QuietHours.parse("21:00-09:00")
    # 18:00 UTC == 23:00 Almaty → must move to 09:00 local next day (04:00 UTC).
    run_at = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)
    shifted = shift_out_of_quiet_hours(run_at, TZ, quiet)
    assert to_tenant(shifted, TZ).hour == 9
    assert shifted > run_at


def test_shift_is_a_noop_outside_quiet_hours():
    quiet = QuietHours.parse("21:00-09:00")
    run_at = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)  # 12:00 local
    assert shift_out_of_quiet_hours(run_at, TZ, quiet) == run_at


def test_shift_without_quiet_hours_is_identity():
    run_at = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)
    assert shift_out_of_quiet_hours(run_at, TZ, None) == run_at


def test_combine_local_and_back():
    moment = combine_local(date(2026, 10, 19), time(9, 0), TZ)
    assert moment == datetime(2026, 10, 19, 4, 0, tzinfo=UTC)
    assert local_date(moment, TZ) == date(2026, 10, 19)
    assert to_utc(datetime(2026, 10, 19, 9, 0), TZ) == moment


def test_local_date_differs_from_utc_date_near_midnight():
    # 20:30 UTC is already the next day in Almaty (+05:00).
    moment = datetime(2026, 10, 19, 20, 30, tzinfo=UTC)
    assert local_date(moment, TZ) == date(2026, 10, 20)


def test_humanize_relative_days(monkeypatch):
    import arix.core.timeutil as tu

    fixed = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)  # 11:00 local
    monkeypatch.setattr(tu, "now_utc", lambda: fixed)
    today = combine_local(date(2026, 10, 19), time(15, 0), TZ)
    tomorrow = combine_local(date(2026, 10, 20), time(10, 0), TZ)
    assert tu.humanize_local(today, TZ) == "сегодня в 15:00"
    assert tu.humanize_local(tomorrow, TZ) == "завтра в 10:00"
    later = combine_local(date(2026, 10, 22), time(9, 0), TZ)
    assert "окт" in tu.humanize_local(later, TZ)


def test_unknown_timezone_falls_back_to_utc():
    moment = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    assert to_tenant(moment, "Mars/Olympus").utcoffset().total_seconds() == 0


def test_month_period_format():
    assert month_period(datetime(2026, 1, 5, tzinfo=UTC)) == "2026-01"


@pytest.mark.parametrize("raw,expected", [("9:5", time(9, 5)), ("09", time(9, 0)), ("23:59", time(23, 59))])
def test_parse_hhmm(raw, expected):
    assert parse_hhmm(raw) == expected
