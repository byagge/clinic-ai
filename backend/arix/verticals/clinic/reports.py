"""Owner reports (ТЗ §9).

Everything is derived from ``events`` and ``visits`` — no separate counters to
drift out of sync.  The daily numbers are also written to ``daily_metrics`` so
the web panel can draw charts without re-scanning the event log.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core import events as ev
from arix.core.context import TenantContext
from arix.core.timeutil import combine_local, local_date, now_utc, to_tenant
from arix.db.models_core import Contact, DailyMetric, Event, Handoff, Payment

from .models import Doctor, Review, Service, Visit


@dataclass
class DailyReport:
    day: date
    conversations: int = 0
    first_response_p50: float | None = None
    first_response_p95: float | None = None
    bookings: int = 0
    bookings_at_night: int = 0
    arrived: int = 0
    expected: int = 0
    no_shows: int = 0
    confirmed_today: int = 0
    to_confirm_today: int = 0
    to_call: list[str] = field(default_factory=list)
    lost_interests: int = 0
    lost_topic: str | None = None
    reviews: dict[int, int] = field(default_factory=dict)
    bad_review_text: str | None = None
    revenue: float = 0.0
    revenue_currency: str = "KZT"
    attribution: dict[str, float] = field(default_factory=dict)
    open_handoffs: int = 0
    queued_handoffs: int = 0

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "conversations": self.conversations,
            "first_response_p50": self.first_response_p50,
            "first_response_p95": self.first_response_p95,
            "bookings": self.bookings,
            "bookings_at_night": self.bookings_at_night,
            "arrived": self.arrived,
            "expected": self.expected,
            "no_shows": self.no_shows,
            "confirmed_today": self.confirmed_today,
            "to_confirm_today": self.to_confirm_today,
            "lost_interests": self.lost_interests,
            "reviews": {str(k): v for k, v in self.reviews.items()},
            "revenue": self.revenue,
            "attribution": self.attribution,
            "open_handoffs": self.open_handoffs,
        }


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return round(ordered[index], 1)


async def build_daily_report(
    session: AsyncSession, ctx: TenantContext, day: date | None = None
) -> DailyReport:
    """Report for ``day`` (tenant-local). Defaults to yesterday."""
    today_local = local_date(now_utc(), ctx.tz_name)
    day = day or today_local - timedelta(days=1)
    start = combine_local(day, datetime.min.time(), ctx.tz_name)
    end = start + timedelta(days=1)
    report = DailyReport(day=day, revenue_currency=ctx.currency)

    rows = (
        await session.execute(
            select(Event).where(
                Event.tenant_id == ctx.id, Event.created_at >= start, Event.created_at < end
            )
        )
    ).scalars()
    response_times: list[float] = []
    conversations: set[uuid.UUID] = set()
    interests: dict[uuid.UUID, str | None] = {}
    converted: set[uuid.UUID] = set()
    for event in rows:
        if event.type == ev.INBOUND and event.conversation_id:
            conversations.add(event.conversation_id)
        elif event.type == ev.FIRST_RESPONSE:
            seconds = (event.data or {}).get("seconds")
            if isinstance(seconds, (int, float)):
                response_times.append(float(seconds))
        elif event.type == ev.BOOKING_CONFIRMED:
            report.bookings += 1
            if event.source == "bot_night":
                report.bookings_at_night += 1
            if event.contact_id:
                converted.add(event.contact_id)
        elif event.type == ev.INTEREST and event.contact_id:
            interests[event.contact_id] = (event.data or {}).get("service")

    report.conversations = len(conversations)
    report.first_response_p50 = _percentile(response_times, 50)
    report.first_response_p95 = _percentile(response_times, 95)

    lost = {cid: svc for cid, svc in interests.items() if cid not in converted}
    report.lost_interests = len(lost)
    if lost:
        topics = [svc for svc in lost.values() if svc]
        report.lost_topic = max(set(topics), key=topics.count) if topics else None

    visits = (
        await session.execute(
            select(Visit).where(
                Visit.tenant_id == ctx.id, Visit.starts_at >= start, Visit.starts_at < end
            )
        )
    ).scalars()
    for visit in visits:
        if visit.status in ("arrived", "done"):
            report.arrived += 1
            report.expected += 1
        elif visit.status == "no_show":
            report.no_shows += 1
            report.expected += 1
        elif visit.status in ("booked", "confirmed"):
            report.expected += 1

    # Today's confirmations and the call list.
    today_start = combine_local(today_local, datetime.min.time(), ctx.tz_name)
    today_visits = (
        await session.execute(
            select(Visit, Contact)
            .join(Contact, Contact.id == Visit.contact_id)
            .where(
                Visit.tenant_id == ctx.id,
                Visit.starts_at >= today_start,
                Visit.starts_at < today_start + timedelta(days=1),
                Visit.status.in_(["booked", "confirmed", "arrived", "done"]),
            )
            .order_by(Visit.starts_at)
        )
    ).all()
    for visit, contact in today_visits:
        report.to_confirm_today += 1
        if visit.status == "booked":
            report.to_call.append(_short_name(contact))
        else:
            report.confirmed_today += 1

    review_rows = (
        await session.execute(
            select(Review).where(
                Review.tenant_id == ctx.id,
                Review.created_at >= start,
                Review.created_at < end,
                Review.score.isnot(None),
            )
        )
    ).scalars()
    worst: Review | None = None
    for review in review_rows:
        report.reviews[review.score] = report.reviews.get(review.score, 0) + 1
        if review.score <= 3 and (worst is None or review.score < (worst.score or 5)):
            worst = review
    if worst is not None:
        contact = (
            await session.execute(select(Contact).where(Contact.id == worst.contact_id))
        ).scalar_one_or_none()
        label = _short_name(contact) if contact else "клиент"
        report.bad_review_text = f"{label}: «{worst.text}»" if worst.text else label

    payments = (
        await session.execute(
            select(Payment.attributed_to, func.sum(Payment.amount))
            .where(
                Payment.tenant_id == ctx.id,
                Payment.status == "paid",
                Payment.paid_at >= start,
                Payment.paid_at < end,
            )
            .group_by(Payment.attributed_to)
        )
    ).all()
    for source, total in payments:
        amount = float(total or 0)
        report.revenue += amount
        report.attribution[source] = amount

    handoffs = (
        await session.execute(
            select(Handoff).where(Handoff.tenant_id == ctx.id, Handoff.resolved_at.is_(None))
        )
    ).scalars()
    for handoff in handoffs:
        report.open_handoffs += 1
        if handoff.queued_until is not None:
            report.queued_handoffs += 1

    await session.execute(
        pg_insert(DailyMetric)
        .values(
            id=uuid.uuid4(), tenant_id=ctx.id, day=day, data=report.as_dict()
        )
        .on_conflict_do_update(
            index_elements=[DailyMetric.tenant_id, DailyMetric.day],
            set_={"data": report.as_dict()},
        )
    )
    return report


def render_daily_report(ctx: TenantContext, report: DailyReport) -> str:
    """The 08:00 Telegram digest from ТЗ §9."""
    from arix.core.timeutil import MONTHS_RU

    cur = {"KZT": "₸", "UZS": "сум", "RUB": "₽"}.get(report.revenue_currency, "")
    head = f"Вчера, {report.day.day} {MONTHS_RU[report.day.month - 1]} · {ctx.name}"
    lines = [head]

    if report.first_response_p50 is not None:
        lines.append(
            f"Обращений {report.conversations} · ответ за {report.first_response_p50:.0f} сек "
            f"(95% — {report.first_response_p95:.0f} сек)"
        )
    else:
        lines.append(f"Обращений {report.conversations}")

    share = (
        f" ({round(report.bookings / report.conversations * 100)}%)"
        if report.conversations
        else ""
    )
    night = f" · из них ночью {report.bookings_at_night}" if report.bookings_at_night else ""
    lines.append(f"Записались {report.bookings}{share}{night}")

    if report.expected:
        lines.append(
            f"Пришли {report.arrived} из {report.expected}"
            + (f" · неявок {report.no_shows}" if report.no_shows else "")
        )
    if report.to_confirm_today:
        tail = (
            " → позвонить: " + ", ".join(report.to_call[:5])
            if report.to_call
            else " — все подтвердили"
        )
        lines.append(
            f"Подтверждены на сегодня {report.confirmed_today} из {report.to_confirm_today}{tail}"
        )
    if report.lost_interests:
        topic = f" ({report.lost_topic})" if report.lost_topic else ""
        lines.append(f"Потерянные: {report.lost_interests}{topic} → бот дожимает")
    if report.reviews:
        parts = [f"{score}★ ×{count}" for score, count in sorted(report.reviews.items(), reverse=True)]
        line = "Отзывы: " + ", ".join(parts)
        if report.bad_review_text:
            line += f" ({report.bad_review_text})"
        lines.append(line)
    if report.revenue:
        attrib = ", ".join(
            f"{_source_ru(src)} {_money(amount)}" for src, amount in report.attribution.items()
        )
        lines.append(f"Оплаты: {_money(report.revenue)} {cur} · {attrib}")
    if report.open_handoffs:
        queued = f" (из них с ночи {report.queued_handoffs})" if report.queued_handoffs else ""
        lines.append(f"Открытых передач: {report.open_handoffs}{queued}")
    return "\n".join(lines)


async def doctor_day_text(
    session: AsyncSession, ctx: TenantContext, doctor: Doctor, day: date
) -> str:
    """Daily schedule for the doctor bot."""
    start = combine_local(day, datetime.min.time(), ctx.tz_name)
    rows = (
        await session.execute(
            select(Visit, Contact, Service)
            .join(Contact, Contact.id == Visit.contact_id)
            .join(Service, Service.id == Visit.service_id)
            .where(
                Visit.tenant_id == ctx.id,
                Visit.doctor_id == doctor.id,
                Visit.starts_at >= start,
                Visit.starts_at < start + timedelta(days=1),
                Visit.status.notin_(["cancelled"]),
            )
            .order_by(Visit.starts_at)
        )
    ).all()
    if not rows:
        return f"{doctor.name}: на {day:%d.%m} записей нет."
    lines = [f"{doctor.name} · {day:%d.%m}"]
    for visit, contact, service in rows:
        mark = {"arrived": "✅", "done": "✅", "no_show": "❌"}.get(visit.status, "•")
        lines.append(
            f"{mark} {to_tenant(visit.starts_at, ctx.tz_name):%H:%M} "
            f"{_short_name(contact)} — {service.label('ru')}"
        )
    return "\n".join(lines)


def _short_name(contact: Contact | None) -> str:
    if contact is None or not contact.name:
        return "без имени"
    parts = contact.name.split()
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[1][0]}."


def _money(value: float) -> str:
    return f"{int(value):,}".replace(",", " ")


def _source_ru(source: str) -> str:
    return {
        "miniapp": "Mini App",
        "followup": "дожим",
        "reactivation": "реактивация",
        "recall": "recall",
        "bot": "бот",
        "bot_night": "бот ночью",
        "admin": "админ",
        "organic": "прочее",
    }.get(source, source)


__all__ = ["DailyReport", "build_daily_report", "doctor_day_text", "render_daily_report"]
