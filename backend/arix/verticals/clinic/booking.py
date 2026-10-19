"""Booking service — the only writer of ``visits``.

Concurrency contract: the ``EXCLUDE USING gist`` constraint on ``visits`` is
the authority on double booking.  We *attempt* the insert and translate the
integrity error into a friendly "slot just went" result.  No read-then-write
check is used, because under 50 concurrent dialogues it would be wrong.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Select, and_, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.timeutil import local_date, now_utc
from arix.db.models_core import Contact

from .models import (
    BLOCKING_STATUSES,
    Doctor,
    DoctorService,
    Schedule,
    ScheduleException,
    Service,
    Visit,
)
from .slots import (
    Busy,
    DayException,
    DaySchedule,
    DoctorAvailability,
    Slot,
    generate_slots,
    merge_doctor_slots,
    spread_slots,
)


class SlotTaken(Exception):
    """Raised when the exclusion constraint rejected the insert."""


@dataclass(frozen=True)
class SlotOffer:
    doctor_id: uuid.UUID
    doctor_name: str
    service_id: uuid.UUID
    service_name: str
    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        """Opaque handle the agent passes back to ``hold_slot``."""
        return f"{self.doctor_id}|{self.service_id}|{self.start.isoformat()}"

    @staticmethod
    def parse_key(key: str) -> tuple[uuid.UUID, uuid.UUID, datetime]:
        doctor, service, start = key.split("|", 2)
        return uuid.UUID(doctor), uuid.UUID(service), datetime.fromisoformat(start)


async def get_service(
    session: AsyncSession, tenant_id: uuid.UUID, service_id: uuid.UUID
) -> Service | None:
    return (
        await session.execute(
            select(Service).where(Service.tenant_id == tenant_id, Service.id == service_id)
        )
    ).scalar_one_or_none()


def _active_services(tenant_id: uuid.UUID) -> Select:
    return select(Service).where(Service.tenant_id == tenant_id, Service.active.is_(True))


async def list_services(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    category: str | None = None,
    query: str | None = None,
    limit: int = 12,
    with_match_flag: bool = False,
) -> list[Service] | tuple[list[Service], bool]:
    """Active services, optionally ranked by a free-text query.

    With ``with_match_flag=True`` also returns whether the query really matched
    something.  That distinction matters: answering "пересадка волос" with the
    price of a consultation would be exactly the invented price ТЗ §0 forbids,
    so the caller must hand over instead.
    """
    stmt = _active_services(tenant_id)
    if category:
        stmt = stmt.where(Service.category.ilike(f"%{category}%"))
    rows = list((await session.execute(stmt.order_by(Service.sort, Service.code))).scalars())
    query_matched = not query
    if query:
        tokens = service_tokens(query)
        if tokens:

            def score(svc: Service) -> int:
                hay = set(
                    service_tokens(
                        " ".join(
                            [
                                *(str(v) for v in svc.name.values()),
                                svc.category or "",
                                svc.code,
                                *svc.complaint_keywords,
                            ]
                        )
                    )
                )
                total = 0
                for token in tokens:
                    if token in hay:
                        total += 10
                    elif len(token) >= 4 and any(word.startswith(token[:4]) for word in hay):
                        # Russian inflections: "имплант" ~ "имплантация".
                        total += 4
                return total

            scored = [(score(svc), svc) for svc in rows]
            hits = [svc for value, svc in sorted(scored, key=lambda p: -p[0]) if value > 0]
            query_matched = bool(hits)
            rows = hits or rows
    if with_match_flag:
        return rows[:limit], query_matched
    return rows[:limit]


#: Words that carry no signal when matching a service name.
SERVICE_STOPWORDS = {
    "сколько", "стоит", "стоимость", "цена", "хочу", "можно", "запишите", "запись",
    "записаться", "мне", "для", "это", "что", "как", "есть", "нужно", "вас", "ваш",
    "про", "почем", "почём", "қанша", "тұрады", "narxi", "qancha", "услуга", "услуги",
}


def service_tokens(text: str) -> list[str]:
    """Punctuation-insensitive tokens used to match a service by free text."""
    words = re.findall(r"[\w\-']+", (text or "").lower(), flags=re.UNICODE)
    return [w for w in words if len(w) > 2 and w not in SERVICE_STOPWORDS]


async def route_by_complaint(
    session: AsyncSession, tenant_id: uuid.UUID, complaint: str, *, limit: int = 3
) -> list[Service]:
    """Map free-text complaint → services. Never a diagnosis, only routing."""
    text = (complaint or "").lower()
    rows = list((await session.execute(_active_services(tenant_id))).scalars())
    scored: list[tuple[int, Service]] = []
    for svc in rows:
        hits = sum(1 for kw in svc.complaint_keywords if kw and kw.lower() in text)
        if hits:
            scored.append((hits * 10 - svc.sort // 100, svc))
    scored.sort(key=lambda p: -p[0])
    if scored:
        return [s for _, s in scored[:limit]]
    # Nothing matched: fall back to services flagged as the entry point.
    consult = [s for s in rows if s.needs_consultation_first or "консульт" in (s.category or "").lower()]
    return consult[:limit]


async def list_doctors(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    service_id: uuid.UUID | None = None,
) -> list[Doctor]:
    stmt = select(Doctor).where(Doctor.tenant_id == tenant_id, Doctor.active.is_(True))
    if service_id:
        stmt = stmt.join(DoctorService, DoctorService.doctor_id == Doctor.id).where(
            DoctorService.service_id == service_id
        )
    return list((await session.execute(stmt.order_by(Doctor.sort, Doctor.name))).scalars())


async def _availability(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    doctor_ids: list[uuid.UUID],
    window_from: datetime,
    window_to: datetime,
) -> dict[uuid.UUID, DoctorAvailability]:
    schedules = list(
        (
            await session.execute(
                select(Schedule).where(
                    Schedule.tenant_id == tenant_id, Schedule.doctor_id.in_(doctor_ids)
                )
            )
        ).scalars()
    )
    exceptions = list(
        (
            await session.execute(
                select(ScheduleException).where(
                    ScheduleException.tenant_id == tenant_id,
                    ScheduleException.doctor_id.in_(doctor_ids),
                    ScheduleException.date >= window_from.date() - timedelta(days=1),
                    ScheduleException.date <= window_to.date() + timedelta(days=1),
                )
            )
        ).scalars()
    )
    busy = list(
        (
            await session.execute(
                select(Visit).where(
                    Visit.tenant_id == tenant_id,
                    Visit.doctor_id.in_(doctor_ids),
                    Visit.status.in_(BLOCKING_STATUSES),
                    Visit.ends_at > window_from,
                    Visit.starts_at < window_to,
                )
            )
        ).scalars()
    )

    out: dict[uuid.UUID, DoctorAvailability] = {
        d: DoctorAvailability(doctor_id=d) for d in doctor_ids
    }
    for s in schedules:
        out[s.doctor_id].weekly.append(
            DaySchedule(s.weekday, s.start, s.end, s.break_start, s.break_end)
        )
    for e in exceptions:
        out[e.doctor_id].exceptions.append(DayException(e.date, e.type, e.start, e.end))
    for v in busy:
        out[v.doctor_id].busy.append(Busy(v.starts_at, v.ends_at))
    return out


async def find_slots(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    service_id: uuid.UUID,
    doctor_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    part_of_day: str | None = None,
    count: int = 3,
    now: datetime | None = None,
    min_lead_minutes: int = 60,
) -> list[SlotOffer]:
    now = now or now_utc()
    service = await get_service(session, ctx.id, service_id)
    if service is None or not service.active:
        return []
    date_from = date_from or local_date(now, ctx.tz_name)
    date_to = date_to or date_from + timedelta(days=14)
    if date_to < date_from:
        date_to = date_from

    doctors = await list_doctors(session, ctx.id, service_id=service_id)
    if doctor_id:
        doctors = [d for d in doctors if d.id == doctor_id]
    if not doctors:
        # Service has no explicit doctor mapping → any active doctor may do it.
        doctors = await list_doctors(session, ctx.id)
        if doctor_id:
            doctors = [d for d in doctors if d.id == doctor_id]
    if not doctors:
        return []

    window_from = now - timedelta(days=1)
    window_to = datetime.combine(date_to + timedelta(days=1), datetime.min.time()).replace(
        tzinfo=now.tzinfo
    )
    availability = await _availability(
        session, ctx.id, [d.id for d in doctors], window_from, window_to
    )

    per_doctor: list[list[Slot]] = []
    for doctor in doctors:
        per_doctor.append(
            generate_slots(
                availability[doctor.id],
                service_id=service_id,
                duration_min=service.duration_min,
                buffer_min=service.buffer_min,
                date_from=date_from,
                date_to=date_to,
                timezone=ctx.tz_name,
                now=now,
                part_of_day=part_of_day,
                min_lead_minutes=min_lead_minutes,
            )
        )
    picked = (
        merge_doctor_slots(per_doctor, count, ctx.tz_name)
        if len(per_doctor) > 1
        else spread_slots(per_doctor[0] if per_doctor else [], count, ctx.tz_name)
    )
    by_id = {d.id: d for d in doctors}
    lang = ctx.default_lang
    return [
        SlotOffer(
            doctor_id=s.doctor_id,
            doctor_name=by_id[s.doctor_id].name,
            service_id=service_id,
            service_name=service.label(lang),
            start=s.start,
            end=s.end,
        )
        for s in picked
    ]


async def hold_slot(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    contact_id: uuid.UUID,
    doctor_id: uuid.UUID,
    service_id: uuid.UUID,
    start: datetime,
    source: str = "bot",
    attribution: str = "bot",
    ttl_minutes: int = 10,
    idempotency_key: str | None = None,
) -> Visit:
    """Create a 10-minute hold. Raises :class:`SlotTaken` on overlap."""
    service = await get_service(session, ctx.id, service_id)
    if service is None:
        raise ValueError("unknown service")
    end = start + timedelta(minutes=service.duration_min)

    if idempotency_key:
        existing = (
            await session.execute(
                select(Visit).where(
                    Visit.tenant_id == ctx.id, Visit.idempotency_key == idempotency_key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    visit = Visit(
        tenant_id=ctx.id,
        contact_id=contact_id,
        doctor_id=doctor_id,
        service_id=service_id,
        starts_at=start,
        ends_at=end,
        # The buffer is reserved on the calendar so the next patient cannot be
        # slotted into the doctor's clean-up gap.
        period=_range(start, end + timedelta(minutes=service.buffer_min)),
        status="hold",
        source=source,
        attribution=attribution,
        hold_expires_at=now_utc() + timedelta(minutes=ttl_minutes),
        idempotency_key=idempotency_key,
        price_quoted=service.price_from,
    )
    # A SAVEPOINT, not a full rollback: an occupied slot is an expected
    # outcome and must not destroy the caller's transaction.
    try:
        async with session.begin_nested():
            session.add(visit)
            await session.flush()
    except IntegrityError as exc:
        raise SlotTaken(str(exc.orig)) from exc
    return visit


def _range(start: datetime, end: datetime) -> Range:
    """Half-open ``tstzrange`` so two touching visits do not count as overlap."""
    return Range(start, end, bounds="[)")


async def confirm_visit(
    session: AsyncSession,
    ctx: TenantContext,
    visit_id: uuid.UUID,
) -> Visit | None:
    visit = (
        await session.execute(
            select(Visit).where(Visit.tenant_id == ctx.id, Visit.id == visit_id)
        )
    ).scalar_one_or_none()
    if visit is None or visit.status not in ("hold", "booked"):
        return visit
    # ``booked`` means the appointment exists. ``confirmed`` (and
    # ``confirmed_at``) means the *patient* confirmed they will come, which
    # only happens when they answer a reminder — see ``set_status``.
    visit.status = "booked"
    visit.hold_expires_at = None
    await session.flush()
    return visit


async def set_status(
    session: AsyncSession,
    ctx: TenantContext,
    visit_id: uuid.UUID,
    status: str,
    *,
    reason: str | None = None,
) -> Visit | None:
    visit = (
        await session.execute(
            select(Visit).where(Visit.tenant_id == ctx.id, Visit.id == visit_id)
        )
    ).scalar_one_or_none()
    if visit is None:
        return None
    visit.status = status
    if reason:
        visit.cancelled_reason = reason[:255]
    if status == "confirmed":
        visit.confirmed_at = now_utc()
    await session.flush()
    return visit


async def reschedule(
    session: AsyncSession,
    ctx: TenantContext,
    visit_id: uuid.UUID,
    new_start: datetime,
    *,
    new_doctor_id: uuid.UUID | None = None,
) -> Visit:
    visit = (
        await session.execute(
            select(Visit).where(Visit.tenant_id == ctx.id, Visit.id == visit_id)
        )
    ).scalar_one_or_none()
    if visit is None:
        raise ValueError("unknown visit")
    service = await get_service(session, ctx.id, visit.service_id)
    assert service is not None
    doctor_id = new_doctor_id or visit.doctor_id
    end = new_start + timedelta(minutes=service.duration_min)
    # Free the old period first so moving inside the same hour cannot self-clash.
    await session.execute(
        update(Visit)
        .where(Visit.id == visit.id)
        .values(status="cancelled", cancelled_reason="rescheduled")
    )
    await session.flush()
    moved = Visit(
        tenant_id=ctx.id,
        contact_id=visit.contact_id,
        doctor_id=doctor_id,
        service_id=visit.service_id,
        starts_at=new_start,
        ends_at=end,
        period=_range(new_start, end + timedelta(minutes=service.buffer_min)),
        status="booked",
        source=visit.source,
        attribution=visit.attribution,
        confirmed_at=now_utc(),
        price_quoted=visit.price_quoted,
        note=f"rescheduled from {visit.starts_at.isoformat()}",
    )
    try:
        async with session.begin_nested():
            session.add(moved)
            await session.flush()
    except IntegrityError as exc:
        raise SlotTaken(str(exc.orig)) from exc
    return moved


async def cancel_visit(
    session: AsyncSession, ctx: TenantContext, visit_id: uuid.UUID, reason: str = "patient"
) -> Visit | None:
    return await set_status(session, ctx, visit_id, "cancelled", reason=reason)


async def upcoming_visits(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    include_past_hours: int = 0,
) -> list[Visit]:
    since = now_utc() - timedelta(hours=include_past_hours)
    return list(
        (
            await session.execute(
                select(Visit)
                .where(
                    Visit.tenant_id == tenant_id,
                    Visit.contact_id == contact_id,
                    Visit.starts_at >= since,
                    Visit.status.in_(["hold", "booked", "confirmed"]),
                )
                .order_by(Visit.starts_at)
            )
        ).scalars()
    )


async def expire_holds(session: AsyncSession, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """Release holds whose TTL passed. Returns the affected visit ids."""
    rows = (
        await session.execute(
            update(Visit)
            .where(
                Visit.tenant_id == tenant_id,
                Visit.status == "hold",
                Visit.hold_expires_at.isnot(None),
                Visit.hold_expires_at <= now_utc(),
            )
            .values(status="cancelled", cancelled_reason="hold_expired")
            .returning(Visit.id)
        )
    ).scalars()
    return list(rows)


async def visits_in_window(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    statuses: list[str] | None = None,
) -> list[Visit]:
    stmt = select(Visit).where(
        Visit.tenant_id == tenant_id, Visit.starts_at >= start, Visit.starts_at < end
    )
    if statuses:
        stmt = stmt.where(Visit.status.in_(statuses))
    return list((await session.execute(stmt.order_by(Visit.starts_at))).scalars())


async def contacts_by_ids(
    session: AsyncSession, tenant_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, Contact]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Contact).where(Contact.tenant_id == tenant_id, Contact.id.in_(ids))
        )
    ).scalars()
    return {c.id: c for c in rows}


async def due_recalls(
    session: AsyncSession, tenant_id: uuid.UUID, *, lead_days: int, now: datetime | None = None
) -> list[Visit]:
    """Completed visits whose ``recall_days`` window opens within ``lead_days``."""
    now = now or now_utc()
    rows = (
        await session.execute(
            select(Visit, Service)
            .join(Service, Service.id == Visit.service_id)
            .where(
                Visit.tenant_id == tenant_id,
                Visit.status == "done",
                Service.recall_days.isnot(None),
            )
        )
    ).all()
    due: list[Visit] = []
    for visit, service in rows:
        target = visit.starts_at + timedelta(days=service.recall_days or 0)
        if now >= target - timedelta(days=lead_days) and now <= target + timedelta(days=30):
            due.append(visit)
    return due


async def has_future_visit(
    session: AsyncSession, tenant_id: uuid.UUID, contact_id: uuid.UUID
) -> bool:
    row = (
        await session.execute(
            select(Visit.id)
            .where(
                Visit.tenant_id == tenant_id,
                Visit.contact_id == contact_id,
                Visit.starts_at > now_utc(),
                Visit.status.in_(["hold", "booked", "confirmed"]),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def dormant_contacts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    months_from: int,
    months_to: int,
    limit: int = 200,
) -> list[Contact]:
    now = now_utc()
    newest = now - timedelta(days=30 * months_from)
    oldest = now - timedelta(days=30 * months_to)
    return list(
        (
            await session.execute(
                select(Contact)
                .where(
                    Contact.tenant_id == tenant_id,
                    Contact.consent_marketing_at.isnot(None),
                    Contact.opted_out_at.is_(None),
                    Contact.anonymized_at.is_(None),
                    Contact.last_visit_at.isnot(None),
                    and_(Contact.last_visit_at <= newest, Contact.last_visit_at >= oldest),
                )
                .limit(limit)
            )
        ).scalars()
    )


async def doctor_day(
    session: AsyncSession, tenant_id: uuid.UUID, doctor_id: uuid.UUID, day_start: datetime
) -> list[Visit]:
    return list(
        (
            await session.execute(
                select(Visit)
                .where(
                    Visit.tenant_id == tenant_id,
                    Visit.doctor_id == doctor_id,
                    Visit.starts_at >= day_start,
                    Visit.starts_at < day_start + timedelta(days=1),
                    Visit.status.notin_(["cancelled"]),
                )
                .order_by(Visit.starts_at)
            )
        ).scalars()
    )


async def open_waitlist_match(
    session: AsyncSession, tenant_id: uuid.UUID, service_id: uuid.UUID, when: datetime
) -> list[uuid.UUID]:
    """Contacts waiting for this service on the freed day (v2 waitlist)."""
    from .models import Waitlist

    day = when.date()
    rows = (
        await session.execute(
            select(Waitlist)
            .where(
                Waitlist.tenant_id == tenant_id,
                Waitlist.service_id == service_id,
                Waitlist.status == "open",
                Waitlist.date_from <= day,
                Waitlist.date_to >= day,
            )
            .order_by(Waitlist.created_at)
            .limit(5)
        )
    ).scalars()
    return [w.contact_id for w in rows]


async def visit_with_refs(
    session: AsyncSession, tenant_id: uuid.UUID, visit_id: uuid.UUID
) -> tuple[Visit, Doctor, Service, Contact] | None:
    row = (
        await session.execute(
            select(Visit, Doctor, Service, Contact)
            .join(Doctor, Doctor.id == Visit.doctor_id)
            .join(Service, Service.id == Visit.service_id)
            .join(Contact, Contact.id == Visit.contact_id)
            .where(Visit.tenant_id == tenant_id, Visit.id == visit_id)
        )
    ).first()
    return tuple(row) if row else None  # type: ignore[return-value]


async def unanswered_reminders(
    session: AsyncSession, tenant_id: uuid.UUID, *, hours_ahead: float
) -> list[Visit]:
    """Visits starting within ``hours_ahead`` that are still unconfirmed."""
    now = now_utc()
    return list(
        (
            await session.execute(
                select(Visit)
                .where(
                    Visit.tenant_id == tenant_id,
                    Visit.status == "booked",
                    Visit.starts_at > now,
                    Visit.starts_at <= now + timedelta(hours=hours_ahead),
                )
                .order_by(Visit.starts_at)
            )
        ).scalars()
    )


def service_price_text(service: Service, lang: str = "ru") -> str:
    """Price string built only from stored values — never guessed."""
    if service.price_from is None:
        return "цена уточняется"
    cur = {"KZT": "₸", "UZS": "сум", "RUB": "₽"}.get(service.currency, service.currency)
    if service.price_to and service.price_to != service.price_from:
        return f"{_num(service.price_from)}–{_num(service.price_to)} {cur}"
    return f"от {_num(service.price_from)} {cur}"


def _num(value) -> str:
    return f"{int(value):,}".replace(",", " ")


__all__ = [
    "SlotOffer",
    "SlotTaken",
    "cancel_visit",
    "confirm_visit",
    "contacts_by_ids",
    "doctor_day",
    "dormant_contacts",
    "due_recalls",
    "expire_holds",
    "find_slots",
    "get_service",
    "has_future_visit",
    "hold_slot",
    "list_doctors",
    "list_services",
    "open_waitlist_match",
    "reschedule",
    "route_by_complaint",
    "service_price_text",
    "set_status",
    "unanswered_reminders",
    "upcoming_visits",
    "visit_with_refs",
    "visits_in_window",
]
