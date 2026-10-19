"""Timezone helpers and quiet hours.

Every stored timestamp is UTC.  Everything a patient sees is rendered in the
tenant's timezone.  Nothing here touches the database, so it is unit-testable
in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MONTHS_RU = (
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)
WEEKDAYS_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - bad tenant config
        return ZoneInfo("UTC")


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_tenant(dt: datetime, timezone: str) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz(timezone))


def to_utc(dt: datetime, timezone: str) -> datetime:
    """Interpret a naive local datetime in the tenant timezone, return UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz(timezone))
    return dt.astimezone(UTC)


def local_date(dt: datetime, timezone: str) -> date:
    return to_tenant(dt, timezone).date()


def combine_local(day: date, t: time, timezone: str) -> datetime:
    """Build a UTC instant from a local calendar day + wall-clock time.

    Uses ``fold=0`` semantics; for the DST-free timezones ARIX targets
    (Asia/Almaty, Asia/Tashkent, Asia/Samarkand) this is unambiguous.
    """
    return datetime.combine(day, t).replace(tzinfo=tz(timezone)).astimezone(UTC)


def parse_hhmm(value: str) -> time:
    hh, _, mm = value.partition(":")
    return time(int(hh), int(mm or 0))


@dataclass(frozen=True)
class QuietHours:
    """Outbound-only silence window, e.g. 21:00 → 09:00 local."""

    start: time
    end: time

    @classmethod
    def parse(cls, spec: str | None) -> QuietHours | None:
        if not spec:
            return None
        raw = spec.replace("—", "-").replace("–", "-")
        start, _, end = raw.partition("-")
        if not end:
            return None
        return cls(parse_hhmm(start.strip()), parse_hhmm(end.strip()))

    @property
    def wraps_midnight(self) -> bool:
        return self.start > self.end

    def contains_local(self, moment: datetime) -> bool:
        t = moment.time()
        if self.wraps_midnight:
            return t >= self.start or t < self.end
        return self.start <= t < self.end

    def next_allowed_local(self, moment: datetime) -> datetime:
        """First instant at or after ``moment`` that is outside the window."""
        if not self.contains_local(moment):
            return moment
        candidate = moment.replace(
            hour=self.end.hour, minute=self.end.minute, second=0, microsecond=0
        )
        if candidate <= moment:
            candidate += timedelta(days=1)
        return candidate


def shift_out_of_quiet_hours(
    run_at_utc: datetime, timezone: str, quiet: QuietHours | None
) -> datetime:
    """Move an outbound job to the first non-quiet moment, preserving order."""
    if quiet is None:
        return run_at_utc
    local = to_tenant(run_at_utc, timezone)
    allowed = quiet.next_allowed_local(local)
    if allowed == local:
        return run_at_utc
    return allowed.astimezone(UTC)


def humanize_local(dt: datetime, timezone: str, lang: str = "ru") -> str:
    """``«завтра в 10:00»``-style rendering used in patient-facing texts."""
    local = to_tenant(dt, timezone)
    today = to_tenant(now_utc(), timezone).date()
    delta = (local.date() - today).days
    hhmm = local.strftime("%H:%M")
    if lang == "ru":
        if delta == 0:
            return f"сегодня в {hhmm}"
        if delta == 1:
            return f"завтра в {hhmm}"
        if 2 <= delta <= 6:
            return f"{WEEKDAYS_RU[local.weekday()]}, {local.day} {MONTHS_RU[local.month - 1]} в {hhmm}"
        return f"{local.day} {MONTHS_RU[local.month - 1]} в {hhmm}"
    if delta == 0:
        return f"today at {hhmm}"
    if delta == 1:
        return f"tomorrow at {hhmm}"
    return f"{local:%d.%m} at {hhmm}"


def month_period(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


__all__ = [
    "QuietHours",
    "combine_local",
    "humanize_local",
    "local_date",
    "month_period",
    "now_utc",
    "parse_hhmm",
    "shift_out_of_quiet_hours",
    "to_tenant",
    "to_utc",
    "tz",
]
