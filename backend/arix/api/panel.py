"""Tenant panel API: calendar, manual bookings, prices, doctors, KB, stats.

Manual (phone) bookings MUST go through ``POST /visits`` — that is the whole
point of the panel.  A visit created here occupies the same exclusion
constraint as a bot booking, so the bot can never double-book over it.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select

from arix.core.events import BOOKING_CONFIRMED, log_event
from arix.core.pii import normalize_phone
from arix.core.timeutil import combine_local, local_date, now_utc, to_tenant
from arix.db.models_core import Contact, Conversation, DailyMetric, Event, Handoff, KBItem, Message
from arix.verticals.clinic import booking as bk
from arix.verticals.clinic.models import (
    Doctor,
    DoctorService,
    Schedule,
    ScheduleException,
    Service,
    Visit,
)
from arix.verticals.clinic.reports import build_daily_report, render_daily_report
from arix.verticals.clinic.scheduling import cancel_visit_jobs, schedule_visit_jobs

from .deps import TenantSession, tenant_scope
from .schemas import (
    DoctorIn,
    DoctorOut,
    KBItemIn,
    ManualBooking,
    PriceQuickPatch,
    ScheduleExceptionIn,
    ScheduleIn,
    ServiceIn,
    ServiceOut,
    SlotOut,
    VisitOut,
    VisitStatusPatch,
)

router = APIRouter(prefix="/panel/{tenant_id}", tags=["panel"])

Scope = Annotated[TenantSession, Depends(tenant_scope)]


# ---------------------------------------------------------------- services --
@router.get("/services", response_model=list[ServiceOut])
async def list_services(scope: Scope) -> list[Service]:
    return list(
        (
            await scope.session.execute(
                select(Service)
                .where(Service.tenant_id == scope.ctx.id)
                .order_by(Service.sort, Service.code)
            )
        ).scalars()
    )


@router.post("/services", response_model=ServiceOut, status_code=201)
async def create_service(body: ServiceIn, scope: Scope) -> Service:
    service = Service(tenant_id=scope.ctx.id, **body.model_dump())
    scope.session.add(service)
    await scope.session.flush()
    return service


@router.put("/services/{service_id}", response_model=ServiceOut)
async def update_service(service_id: uuid.UUID, body: ServiceIn, scope: Scope) -> Service:
    service = await bk.get_service(scope.session, scope.ctx.id, service_id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown service")
    for key, value in body.model_dump().items():
        setattr(service, key, value)
    await scope.session.flush()
    return service


@router.post("/prices/quick")
async def quick_price(body: PriceQuickPatch, scope: Scope) -> dict:
    """What ``/prices`` in the owner bot calls."""
    service = (
        await scope.session.execute(
            select(Service).where(
                Service.tenant_id == scope.ctx.id, Service.code == body.service_code
            )
        )
    ).scalar_one_or_none()
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown service code")
    if body.price_from is not None:
        service.price_from = body.price_from
    if body.price_to is not None:
        service.price_to = body.price_to
    await scope.session.flush()
    return {
        "code": service.code,
        "price_from": float(service.price_from or 0),
        "price_to": float(service.price_to or 0),
    }


# ----------------------------------------------------------------- doctors --
@router.get("/doctors", response_model=list[DoctorOut])
async def list_doctors(scope: Scope) -> list[Doctor]:
    return await bk.list_doctors(scope.session, scope.ctx.id)


@router.post("/doctors", response_model=DoctorOut, status_code=201)
async def create_doctor(body: DoctorIn, scope: Scope) -> Doctor:
    payload = body.model_dump()
    service_ids = payload.pop("service_ids", [])
    doctor = Doctor(tenant_id=scope.ctx.id, **payload)
    scope.session.add(doctor)
    await scope.session.flush()
    for service_id in service_ids:
        scope.session.add(
            DoctorService(
                tenant_id=scope.ctx.id, doctor_id=doctor.id, service_id=service_id
            )
        )
    await scope.session.flush()
    return doctor


@router.put("/doctors/{doctor_id}", response_model=DoctorOut)
async def update_doctor(doctor_id: uuid.UUID, body: DoctorIn, scope: Scope) -> Doctor:
    doctor = (
        await scope.session.execute(
            select(Doctor).where(Doctor.tenant_id == scope.ctx.id, Doctor.id == doctor_id)
        )
    ).scalar_one_or_none()
    if doctor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown doctor")
    payload = body.model_dump()
    service_ids = payload.pop("service_ids", None)
    for key, value in payload.items():
        setattr(doctor, key, value)
    if service_ids is not None:
        await scope.session.execute(
            delete(DoctorService).where(DoctorService.doctor_id == doctor_id)
        )
        for service_id in service_ids:
            scope.session.add(
                DoctorService(
                    tenant_id=scope.ctx.id, doctor_id=doctor_id, service_id=service_id
                )
            )
    await scope.session.flush()
    return doctor


# --------------------------------------------------------------- schedules --
@router.get("/schedules")
async def list_schedules(scope: Scope, doctor_id: uuid.UUID | None = None) -> dict:
    stmt = select(Schedule).where(Schedule.tenant_id == scope.ctx.id)
    if doctor_id:
        stmt = stmt.where(Schedule.doctor_id == doctor_id)
    weekly = list((await scope.session.execute(stmt.order_by(Schedule.weekday))).scalars())
    exc_stmt = select(ScheduleException).where(
        ScheduleException.tenant_id == scope.ctx.id,
        ScheduleException.date >= local_date(now_utc(), scope.ctx.tz_name),
    )
    if doctor_id:
        exc_stmt = exc_stmt.where(ScheduleException.doctor_id == doctor_id)
    exceptions = list((await scope.session.execute(exc_stmt)).scalars())
    return {
        "weekly": [
            {
                "id": str(row.id),
                "doctor_id": str(row.doctor_id),
                "weekday": row.weekday,
                "start": row.start.isoformat(),
                "end": row.end.isoformat(),
                "break_start": row.break_start.isoformat() if row.break_start else None,
                "break_end": row.break_end.isoformat() if row.break_end else None,
            }
            for row in weekly
        ],
        "exceptions": [
            {
                "id": str(row.id),
                "doctor_id": str(row.doctor_id),
                "date": row.date.isoformat(),
                "type": row.type,
                "start": row.start.isoformat() if row.start else None,
                "end": row.end.isoformat() if row.end else None,
                "note": row.note,
            }
            for row in exceptions
        ],
    }


@router.post("/schedules", status_code=201)
async def create_schedule(body: ScheduleIn, scope: Scope) -> dict:
    row = Schedule(tenant_id=scope.ctx.id, **body.model_dump())
    scope.session.add(row)
    await scope.session.flush()
    return {"id": str(row.id)}


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: uuid.UUID, scope: Scope) -> None:
    await scope.session.execute(
        delete(Schedule).where(
            Schedule.tenant_id == scope.ctx.id, Schedule.id == schedule_id
        )
    )


@router.post("/schedule-exceptions", status_code=201)
async def create_exception(body: ScheduleExceptionIn, scope: Scope) -> dict:
    """``/off`` in the owner bot: "врач не работает в этот день"."""
    row = ScheduleException(tenant_id=scope.ctx.id, **body.model_dump())
    scope.session.add(row)
    await scope.session.flush()
    # Visits already booked into the removed window need a human decision.
    day_start = combine_local(body.date, datetime.min.time(), scope.ctx.tz_name)
    clashing = await bk.visits_in_window(
        scope.session, scope.ctx.id, day_start, day_start + timedelta(days=1),
        statuses=["booked", "confirmed", "hold"],
    )
    affected = [str(v.id) for v in clashing if v.doctor_id == body.doctor_id]
    return {"id": str(row.id), "affected_visits": affected}


# ------------------------------------------------------------------- slots --
@router.get("/slots", response_model=list[SlotOut])
async def slots(
    scope: Scope,
    service_id: uuid.UUID,
    doctor_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    part_of_day: str | None = None,
    count: int = Query(default=12, le=50),
) -> list[SlotOut]:
    offers = await bk.find_slots(
        scope.session, scope.ctx,
        service_id=service_id, doctor_id=doctor_id,
        date_from=date_from, date_to=date_to, part_of_day=part_of_day, count=count,
        min_lead_minutes=0,
    )
    tz = scope.ctx.tz_name
    return [
        SlotOut(
            slot_key=o.key, start=o.start, end=o.end, doctor_id=o.doctor_id,
            doctor=o.doctor_name, service_id=o.service_id, service=o.service_name,
            time=to_tenant(o.start, tz).strftime("%H:%M"),
            date=to_tenant(o.start, tz).strftime("%Y-%m-%d"),
        )
        for o in offers
    ]


# ------------------------------------------------------------------ visits --
@router.get("/visits", response_model=list[VisitOut])
async def list_visits(
    scope: Scope,
    day: date | None = None,
    days: int = Query(default=1, ge=1, le=31),
    doctor_id: uuid.UUID | None = None,
) -> list[VisitOut]:
    tz = scope.ctx.tz_name
    day = day or local_date(now_utc(), tz)
    start = combine_local(day, datetime.min.time(), tz)
    rows = (
        await scope.session.execute(
            select(Visit, Doctor, Service, Contact)
            .join(Doctor, Doctor.id == Visit.doctor_id)
            .join(Service, Service.id == Visit.service_id)
            .join(Contact, Contact.id == Visit.contact_id)
            .where(
                Visit.tenant_id == scope.ctx.id,
                Visit.starts_at >= start,
                Visit.starts_at < start + timedelta(days=days),
                *( [Visit.doctor_id == doctor_id] if doctor_id else [] ),
            )
            .order_by(Visit.starts_at)
        )
    ).all()
    return [
        VisitOut(
            id=visit.id, starts_at=visit.starts_at, ends_at=visit.ends_at,
            status=visit.status, source=visit.source, attribution=visit.attribution,
            doctor_id=doctor.id, doctor=doctor.name,
            service_id=service.id, service=service.label("ru"),
            contact_id=contact.id, contact_name=contact.name, contact_phone=contact.phone,
        )
        for visit, doctor, service, contact in rows
    ]


@router.post("/visits", response_model=VisitOut, status_code=201)
async def create_manual_visit(body: ManualBooking, scope: Scope) -> VisitOut:
    """Add a phone booking. Returns 409 when the slot is already taken."""
    session, ctx = scope.session, scope.ctx
    contact_id = body.contact_id
    if contact_id is None:
        phone = normalize_phone(body.contact_phone, ctx.tenant.country)
        contact = None
        if phone:
            contact = (
                await session.execute(
                    select(Contact).where(Contact.tenant_id == ctx.id, Contact.phone == phone)
                )
            ).scalar_one_or_none()
        if contact is None:
            contact = Contact(
                tenant_id=ctx.id, phone=phone, name=body.contact_name,
                lang=ctx.default_lang, source="admin", consent_pd_at=now_utc(),
            )
            session.add(contact)
            await session.flush()
        contact_id = contact.id

    try:
        visit = await bk.hold_slot(
            session, ctx,
            contact_id=contact_id, doctor_id=body.doctor_id, service_id=body.service_id,
            start=body.start, source="admin", attribution="admin",
            idempotency_key=f"admin:{body.doctor_id}:{body.start.isoformat()}",
        )
    except bk.SlotTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slot is already taken") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    visit.status = body.status if body.status in ("booked", "confirmed") else "booked"
    visit.hold_expires_at = None
    visit.confirmed_at = now_utc() if visit.status == "confirmed" else None
    visit.note = body.note
    await session.flush()
    await schedule_visit_jobs(session, ctx, visit.id)
    await log_event(
        session, ctx.id, BOOKING_CONFIRMED, contact_id=contact_id,
        entity_ref=str(visit.id), source="admin",
    )

    refs = await bk.visit_with_refs(session, ctx.id, visit.id)
    assert refs is not None
    v, doctor, service, contact = refs
    return VisitOut(
        id=v.id, starts_at=v.starts_at, ends_at=v.ends_at, status=v.status,
        source=v.source, attribution=v.attribution,
        doctor_id=doctor.id, doctor=doctor.name,
        service_id=service.id, service=service.label("ru"),
        contact_id=contact.id, contact_name=contact.name, contact_phone=contact.phone,
    )


@router.patch("/visits/{visit_id}/status")
async def patch_visit_status(
    visit_id: uuid.UUID, body: VisitStatusPatch, scope: Scope
) -> dict:
    from arix.verticals.clinic.handlers import schedule_noshow_rebook
    from arix.verticals.clinic.scheduling import schedule_after_visit

    allowed = ("confirmed", "arrived", "no_show", "cancelled", "done")
    if body.status not in allowed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"status must be one of {allowed}")
    visit = await bk.set_status(scope.session, scope.ctx, visit_id, body.status, reason=body.reason)
    if visit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown visit")

    if body.status == "done":
        contact = (
            await scope.session.execute(select(Contact).where(Contact.id == visit.contact_id))
        ).scalar_one()
        contact.last_visit_at = visit.starts_at
        await schedule_after_visit(scope.session, scope.ctx, visit_id)
    elif body.status == "no_show":
        await schedule_noshow_rebook(scope.session, scope.ctx, visit_id)
        await cancel_visit_jobs(scope.session, scope.ctx, visit_id)
    elif body.status == "cancelled":
        await cancel_visit_jobs(scope.session, scope.ctx, visit_id)
    return {"id": str(visit.id), "status": visit.status}


@router.post("/visits/{visit_id}/reschedule", response_model=VisitOut)
async def reschedule_visit(visit_id: uuid.UUID, slot_key: str, scope: Scope) -> VisitOut:
    try:
        doctor_id, _service_id, start = bk.SlotOffer.parse_key(slot_key)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad slot_key") from exc
    try:
        moved = await bk.reschedule(
            scope.session, scope.ctx, visit_id, start, new_doctor_id=doctor_id
        )
    except bk.SlotTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slot is already taken") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown visit") from exc
    await cancel_visit_jobs(scope.session, scope.ctx, visit_id)
    await schedule_visit_jobs(scope.session, scope.ctx, moved.id)
    refs = await bk.visit_with_refs(scope.session, scope.ctx.id, moved.id)
    assert refs is not None
    v, doctor, service, contact = refs
    return VisitOut(
        id=v.id, starts_at=v.starts_at, ends_at=v.ends_at, status=v.status,
        source=v.source, attribution=v.attribution, doctor_id=doctor.id, doctor=doctor.name,
        service_id=service.id, service=service.label("ru"),
        contact_id=contact.id, contact_name=contact.name, contact_phone=contact.phone,
    )


# ---------------------------------------------------------------- dialogs ---
@router.get("/conversations")
async def list_conversations(
    scope: Scope,
    state: str | None = None,
    assigned: str | None = None,
    limit: int = Query(default=50, le=200),
) -> list[dict]:
    stmt = (
        select(Conversation, Contact)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.tenant_id == scope.ctx.id)
        .order_by(Conversation.last_inbound_at.desc().nullslast())
        .limit(limit)
    )
    if state:
        stmt = stmt.where(Conversation.state == state)
    if assigned:
        stmt = stmt.where(Conversation.assigned_to == assigned)
    rows = (await scope.session.execute(stmt)).all()
    return [
        {
            "id": str(conversation.id),
            "channel": conversation.channel_type,
            "state": conversation.state,
            "assigned_to": conversation.assigned_to,
            "last_inbound_at": conversation.last_inbound_at,
            "contact": {
                "id": str(contact.id),
                "name": contact.name,
                "phone": contact.phone,
                "lang": contact.lang,
            },
        }
        for conversation, contact in rows
    ]


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: uuid.UUID, scope: Scope, limit: int = Query(default=100, le=500)
) -> list[dict]:
    rows = (
        await scope.session.execute(
            select(Message)
            .where(
                Message.tenant_id == scope.ctx.id, Message.conversation_id == conversation_id
            )
            .order_by(Message.seq)
            .limit(limit)
        )
    ).scalars()
    return [
        {
            "id": str(row.id),
            "direction": row.direction,
            "type": row.type,
            "text": row.stt_text or row.text,
            "sender": row.sender,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/conversations/{conversation_id}/reply")
async def manual_reply(conversation_id: uuid.UUID, text: str, scope: Scope) -> dict:
    """Admin writes to the patient from the panel; the bot stands down."""
    from arix.domain.conversations import assign_to_human
    from arix.domain.messaging import send_message

    conversation = (
        await scope.session.execute(
            select(Conversation).where(
                Conversation.tenant_id == scope.ctx.id, Conversation.id == conversation_id
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown conversation")
    await assign_to_human(scope.session, conversation, None)
    outcome = await send_message(
        scope.session, scope.ctx, conversation, text, sender="admin"
    )
    return {"ok": outcome.ok, "error": outcome.error or outcome.skipped_reason}


@router.post("/conversations/{conversation_id}/return-to-bot")
async def return_to_bot(conversation_id: uuid.UUID, scope: Scope) -> dict:
    from arix.domain.conversations import return_to_bot as _return

    conversation = (
        await scope.session.execute(
            select(Conversation).where(
                Conversation.tenant_id == scope.ctx.id, Conversation.id == conversation_id
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown conversation")
    await _return(scope.session, conversation)
    return {"ok": True}


@router.get("/handoffs")
async def list_handoffs(scope: Scope, open_only: bool = True) -> list[dict]:
    stmt = select(Handoff).where(Handoff.tenant_id == scope.ctx.id)
    if open_only:
        stmt = stmt.where(Handoff.resolved_at.is_(None))
    rows = (await scope.session.execute(stmt.order_by(Handoff.created_at.desc()).limit(100))).scalars()
    return [
        {
            "id": str(row.id),
            "number": row.number,
            "reason": row.reason,
            "priority": row.priority,
            "summary": row.summary,
            "conversation_id": str(row.conversation_id),
            "created_at": row.created_at,
            "taken_at": row.taken_at,
            "resolved_at": row.resolved_at,
        }
        for row in rows
    ]


# --------------------------------------------------------------------- KB ---
@router.get("/kb")
async def list_kb(scope: Scope) -> list[dict]:
    rows = (
        await scope.session.execute(
            select(KBItem).where(KBItem.tenant_id == scope.ctx.id).order_by(KBItem.type)
        )
    ).scalars()
    return [
        {
            "id": str(row.id),
            "type": row.type,
            "question": row.question,
            "answer": row.answer,
            "lang": row.lang,
            "verbatim": row.verbatim,
            "active": row.active,
        }
        for row in rows
    ]


@router.post("/kb", status_code=201)
async def create_kb(body: KBItemIn, scope: Scope) -> dict:
    row = KBItem(tenant_id=scope.ctx.id, **body.model_dump())
    scope.session.add(row)
    await scope.session.flush()
    return {"id": str(row.id)}


@router.put("/kb/{item_id}")
async def update_kb(item_id: uuid.UUID, body: KBItemIn, scope: Scope) -> dict:
    row = (
        await scope.session.execute(
            select(KBItem).where(KBItem.tenant_id == scope.ctx.id, KBItem.id == item_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown kb item")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await scope.session.flush()
    return {"id": str(row.id)}


@router.delete("/kb/{item_id}", status_code=204)
async def delete_kb(item_id: uuid.UUID, scope: Scope) -> None:
    await scope.session.execute(
        delete(KBItem).where(KBItem.tenant_id == scope.ctx.id, KBItem.id == item_id)
    )


# ------------------------------------------------------------------ stats ---
@router.get("/report/daily")
async def daily_report(scope: Scope, day: date | None = None) -> dict:
    report = await build_daily_report(scope.session, scope.ctx, day)
    return {"data": report.as_dict(), "text": render_daily_report(scope.ctx, report)}


@router.get("/stats/funnel")
async def funnel(scope: Scope, days: int = Query(default=7, ge=1, le=90)) -> dict:
    since = now_utc() - timedelta(days=days)
    rows = (
        await scope.session.execute(
            select(Event.type, func.count())
            .where(Event.tenant_id == scope.ctx.id, Event.created_at >= since)
            .group_by(Event.type)
        )
    ).all()
    counts = {event_type: count for event_type, count in rows}
    handoff_reasons = (
        await scope.session.execute(
            select(Handoff.reason, func.count())
            .where(Handoff.tenant_id == scope.ctx.id, Handoff.created_at >= since)
            .group_by(Handoff.reason)
        )
    ).all()
    metrics = (
        await scope.session.execute(
            select(DailyMetric)
            .where(DailyMetric.tenant_id == scope.ctx.id)
            .order_by(DailyMetric.day.desc())
            .limit(days)
        )
    ).scalars()
    return {
        "window_days": days,
        "events": counts,
        "handoff_reasons": {reason: count for reason, count in handoff_reasons},
        "daily": [row.data for row in metrics],
    }


__all__ = ["router"]
