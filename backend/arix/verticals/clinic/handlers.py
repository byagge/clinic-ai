"""Clinic job handlers (ТЗ §8)."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.channels.base import Button
from arix.core.context import TenantContext
from arix.core.events import RECALL_SENT, VISIT_DONE, log_event
from arix.core.timeutil import humanize_local, local_date, now_utc, to_tenant
from arix.db.models_core import Contact, Conversation, ScheduledJob
from arix.domain.messaging import send_message
from arix.domain.notify import notify_staff
from arix.logging_setup import get_logger
from arix.templates import render

from . import booking as bk
from .models import Service, Visit
from .reports import build_daily_report, render_daily_report
from .reviews import request_review
from .scheduling import schedule_after_visit, schedule_noshow_rebook, schedule_owner_report

log = get_logger("arix.clinic.jobs")


async def _visit_bundle(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> tuple[Visit, Contact, Conversation, Service] | None:
    raw = (job.payload or {}).get("visit_id")
    if not raw:
        return None
    refs = await bk.visit_with_refs(session, ctx.id, uuid.UUID(raw))
    if refs is None:
        return None
    visit, _doctor, service, contact = refs
    conversation = await _latest_conversation(session, ctx, contact.id)
    if conversation is None:
        return None
    return visit, contact, conversation, service


async def _latest_conversation(
    session: AsyncSession, ctx: TenantContext, contact_id: uuid.UUID
) -> Conversation | None:
    return (
        await session.execute(
            select(Conversation)
            .where(Conversation.tenant_id == ctx.id, Conversation.contact_id == contact_id)
            .order_by(Conversation.last_inbound_at.desc().nullslast())
            .limit(1)
        )
    ).scalar_one_or_none()


async def reminder_24h(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    bundle = await _visit_bundle(session, ctx, job)
    if bundle is None:
        return {"skipped": "no_visit"}
    visit, contact, conversation, service = bundle
    if visit.status != "booked":
        # ``confirmed`` means the patient already answered a reminder.
        return {"skipped": f"status_{visit.status}"}

    refs = await bk.visit_with_refs(session, ctx.id, visit.id)
    doctor_name = refs[1].name if refs else ""
    text = render(
        "reminder_24h", contact.lang, overrides=ctx.settings.texts,
        when=humanize_local(visit.starts_at, ctx.tz_name, contact.lang),
        service=service.label(contact.lang), doctor=doctor_name,
        address=ctx.settings.address or "",
    )
    outcome = await send_message(
        session, ctx, conversation, text, contact=contact,
        buttons=[
            Button("Приду", f"visit:confirm:{visit.id}"),
            Button("Перенести", f"visit:reschedule:{visit.id}"),
            Button("Отменить", f"visit:cancel:{visit.id}"),
        ],
    )
    return {"sent": outcome.ok, "reason": outcome.skipped_reason}


async def reminder_2h(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    bundle = await _visit_bundle(session, ctx, job)
    if bundle is None:
        return {"skipped": "no_visit"}
    visit, contact, conversation, _service = bundle
    if visit.status not in ("booked", "confirmed"):
        return {"skipped": f"status_{visit.status}"}
    text = render(
        "reminder_2h", contact.lang, overrides=ctx.settings.texts,
        when=humanize_local(visit.starts_at, ctx.tz_name, contact.lang),
        address=ctx.settings.address or "",
        map_line=f"Как добраться: {ctx.settings.map_url}" if ctx.settings.map_url else "",
    )
    outcome = await send_message(session, ctx, conversation, text, contact=contact)
    return {"sent": outcome.ok}


async def call_task_admin(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    """−24h reminder went unanswered → the admin should phone the patient."""
    bundle = await _visit_bundle(session, ctx, job)
    if bundle is None:
        return {"skipped": "no_visit"}
    visit, contact, _conversation, service = bundle
    if visit.status != "booked":
        return {"skipped": f"status_{visit.status}"}
    sent = await notify_staff(
        session, ctx, roles=["admin"],
        text=(
            f"📞 Позвонить: {contact.name or 'без имени'}"
            + (f" · {contact.phone}" if contact.phone else "")
            + f"\nНе подтвердил визит {to_tenant(visit.starts_at, ctx.tz_name):%d.%m %H:%M}"
            f" · {service.label('ru')}"
        ),
    )
    return {"notified": sent}


async def hold_expire(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    bundle = await _visit_bundle(session, ctx, job)
    if bundle is None:
        return {"skipped": "no_visit"}
    visit, contact, conversation, _service = bundle
    if visit.status != "hold":
        return {"skipped": f"status_{visit.status}"}
    await bk.set_status(session, ctx, visit.id, "cancelled", reason="hold_expired")
    scratch = dict(conversation.scratch or {})
    if scratch.get("pending_visit_id") == str(visit.id):
        scratch["pending_visit_id"] = None
        scratch["still_needed"] = []
        conversation.scratch = scratch
    text = render(
        "hold_expired", contact.lang, overrides=ctx.settings.texts,
        when=humanize_local(visit.starts_at, ctx.tz_name, contact.lang),
    )
    outcome = await send_message(session, ctx, conversation, text, contact=contact)
    return {"released": True, "notified": outcome.ok}


async def visit_autocomplete(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> dict:
    """2h after the visit ended, treat it as done unless marked otherwise."""
    bundle = await _visit_bundle(session, ctx, job)
    if bundle is None:
        return {"skipped": "no_visit"}
    visit, contact, _conversation, service = bundle
    if visit.status in ("no_show", "cancelled", "done"):
        return {"skipped": f"status_{visit.status}"}
    await bk.set_status(session, ctx, visit.id, "done")
    contact.last_visit_at = visit.starts_at
    await session.flush()
    await log_event(
        session, ctx.id, VISIT_DONE, contact_id=contact.id, entity_ref=str(visit.id),
        amount=float(visit.price_quoted) if visit.price_quoted else None,
        currency=service.currency, source=visit.attribution,
    )
    await schedule_after_visit(session, ctx, visit.id)
    return {"completed": True}


async def review_request(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    bundle = await _visit_bundle(session, ctx, job)
    if bundle is None:
        return {"skipped": "no_visit"}
    visit, contact, conversation, _service = bundle
    if visit.status != "done":
        return {"skipped": f"status_{visit.status}"}
    ok = await request_review(session, ctx, visit, conversation, contact)
    return {"sent": ok}


async def recall_invite(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    bundle = await _visit_bundle(session, ctx, job)
    if bundle is None:
        return {"skipped": "no_visit"}
    visit, contact, conversation, service = bundle
    if contact.opted_out_at is not None:
        return {"skipped": "opted_out"}
    if await bk.has_future_visit(session, ctx.id, contact.id):
        return {"skipped": "already_booked"}

    offers = await bk.find_slots(
        session, ctx, service_id=service.id, count=3,
        date_from=local_date(now_utc(), ctx.tz_name),
    )
    if not offers:
        return {"skipped": "no_slots"}
    months = round((service.recall_days or 180) / 30)
    text = render(
        "recall_invite", contact.lang, overrides=ctx.settings.texts,
        name=(contact.name or "").split()[0] if contact.name else "Здравствуйте",
        months=months, service=service.label(contact.lang),
        slots=", ".join(humanize_local(o.start, ctx.tz_name, contact.lang) for o in offers),
    )
    outcome = await send_message(
        session, ctx, conversation, text, contact=contact,
        buttons=[
            Button(to_tenant(o.start, ctx.tz_name).strftime("%d.%m %H:%M"), f"slot:{o.key}")
            for o in offers
        ],
    )
    if outcome.ok:
        scratch = dict(conversation.scratch or {})
        scratch["attribution"] = "recall"
        scratch["offered_slots"] = [
            {
                "slot_key": o.key,
                "time": to_tenant(o.start, ctx.tz_name).strftime("%H:%M"),
                "doctor": o.doctor_name,
                "when": humanize_local(o.start, ctx.tz_name, contact.lang),
            }
            for o in offers
        ]
        conversation.scratch = scratch
        await log_event(
            session, ctx.id, RECALL_SENT, contact_id=contact.id,
            conversation_id=conversation.id, source="recall",
            entity_ref=str(visit.id),
        )
    return {"sent": outcome.ok}


async def dormant_invite(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    """"Давно не были" — marketing consent required (ТЗ §7 С9)."""
    recall = ctx.settings.recall
    contacts = await bk.dormant_contacts(
        session, ctx.id,
        months_from=recall.dormant_months_from, months_to=recall.dormant_months_to,
        limit=recall.dormant_max_per_month * 100,
    )
    sent = 0
    for contact in contacts:
        conversation = await _latest_conversation(session, ctx, contact.id)
        if conversation is None:
            continue
        if await bk.has_future_visit(session, ctx.id, contact.id):
            continue
        text = render(
            "dormant_invite", contact.lang, overrides=ctx.settings.texts,
            name=(contact.name or "").split()[0] if contact.name else "Здравствуйте",
        )
        outcome = await send_message(
            session, ctx, conversation, text, contact=contact,
            is_marketing=True,
            buttons=[Button("Не писать", "optout")],
        )
        if outcome.ok:
            sent += 1
            await log_event(
                session, ctx.id, RECALL_SENT, contact_id=contact.id,
                conversation_id=conversation.id, source="reactivation",
            )
    return {"sent": sent, "candidates": len(contacts)}


async def noshow_rebook(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    bundle = await _visit_bundle(session, ctx, job)
    if bundle is None:
        return {"skipped": "no_visit"}
    visit, contact, conversation, service = bundle
    if visit.status != "no_show":
        return {"skipped": f"status_{visit.status}"}
    if await bk.has_future_visit(session, ctx.id, contact.id):
        return {"skipped": "already_rebooked"}
    offers = await bk.find_slots(session, ctx, service_id=service.id, count=2)
    text = render(
        "noshow_rebook", contact.lang, overrides=ctx.settings.texts,
        slots=", ".join(humanize_local(o.start, ctx.tz_name, contact.lang) for o in offers)
        or "свободное время",
    )
    outcome = await send_message(
        session, ctx, conversation, text, contact=contact,
        buttons=[
            Button(to_tenant(o.start, ctx.tz_name).strftime("%d.%m %H:%M"), f"slot:{o.key}")
            for o in offers
        ],
    )
    return {"sent": outcome.ok}


async def owner_report(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    report = await build_daily_report(session, ctx)
    text = render_daily_report(ctx, report)
    sent = await notify_staff(
        session, ctx, roles=["owner", "admin"], text=text,
        buttons=[("Сегодня", "rep:today"), ("Потерянные", "rep:lost"), ("Отзывы", "rep:reviews")],
    )
    # Arm tomorrow's report and the night digest.
    await schedule_owner_report(session, ctx)
    return {"sent": sent, "day": report.day.isoformat()}


async def waitlist_offer(session: AsyncSession, ctx: TenantContext, job: ScheduledJob) -> dict:
    """A slot was freed → offer it to the first person waiting (v2)."""
    if not ctx.settings.features.waitlist:
        return {"skipped": "disabled"}
    payload = job.payload or {}
    service_id = uuid.UUID(payload["service_id"])
    when = now_utc() + timedelta(seconds=0)
    candidates = await bk.open_waitlist_match(session, ctx.id, service_id, when)
    for contact_id in candidates[:1]:
        contact = (
            await session.execute(select(Contact).where(Contact.id == contact_id))
        ).scalar_one_or_none()
        conversation = await _latest_conversation(session, ctx, contact_id) if contact else None
        if not (contact and conversation):
            continue
        offers = await bk.find_slots(session, ctx, service_id=service_id, count=2)
        if not offers:
            return {"skipped": "no_slots"}
        await send_message(
            session, ctx, conversation,
            "Освободилось время — "
            + ", ".join(humanize_local(o.start, ctx.tz_name, contact.lang) for o in offers)
            + ". Записать?",
            contact=contact,
            buttons=[
                Button(to_tenant(o.start, ctx.tz_name).strftime("%d.%m %H:%M"), f"slot:{o.key}")
                for o in offers
            ],
        )
        return {"offered": 1}
    return {"offered": 0}


async def treatment_plan_nudge(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> dict:
    """Overdue treatment-plan step (ТЗ §7 С10, v2)."""
    if not ctx.settings.features.treatment_plans:
        return {"skipped": "disabled"}
    from .models import TreatmentPlan

    plans = list(
        (
            await session.execute(
                select(TreatmentPlan).where(
                    TreatmentPlan.tenant_id == ctx.id, TreatmentPlan.status == "active"
                )
            )
        ).scalars()
    )
    nudged = 0
    escalated = 0
    today = local_date(now_utc(), ctx.tz_name)
    for plan in plans:
        steps = (plan.steps or {}).get("items", [])
        overdue_days = 0
        for step in steps:
            if step.get("status") == "done" or not step.get("due_date"):
                continue
            from datetime import date as _date

            try:
                due = _date.fromisoformat(step["due_date"])
            except ValueError:
                continue
            overdue_days = max(overdue_days, (today - due).days)
        if overdue_days < 14:
            continue
        contact = (
            await session.execute(select(Contact).where(Contact.id == plan.contact_id))
        ).scalar_one_or_none()
        conversation = (
            await _latest_conversation(session, ctx, plan.contact_id) if contact else None
        )
        if not (contact and conversation):
            continue
        if overdue_days >= 30:
            await notify_staff(
                session, ctx, roles=["admin", "doctor"],
                text=f"🗂 План лечения «{plan.title}» просрочен на {overdue_days} дн. "
                f"Клиент: {contact.name or 'без имени'}",
            )
            escalated += 1
        else:
            await send_message(
                session, ctx, conversation,
                f"По плану «{plan.title}» остался следующий этап. Подобрать время?",
                contact=contact,
            )
            nudged += 1
    return {"nudged": nudged, "escalated": escalated}


# --- providers the core handlers ask for -----------------------------------


async def slot_hint(session: AsyncSession, ctx: TenantContext, payload: dict) -> str | None:
    """Two concrete times for a follow-up message."""
    service_name = payload.get("service")
    services = await bk.list_services(session, ctx.id, query=service_name, limit=1)
    if not services:
        return None
    offers = await bk.find_slots(session, ctx, service_id=services[0].id, count=2)
    if not offers:
        return None
    return ", ".join(humanize_local(o.start, ctx.tz_name, ctx.default_lang) for o in offers)


CLINIC_HANDLERS = {
    "reminder_24h": reminder_24h,
    "reminder_2h": reminder_2h,
    "call_task_admin": call_task_admin,
    "hold_expire": hold_expire,
    "visit_autocomplete": visit_autocomplete,
    "review_request": review_request,
    "recall_invite": recall_invite,
    "dormant_invite": dormant_invite,
    "noshow_rebook": noshow_rebook,
    "owner_report": owner_report,
    "waitlist_offer": waitlist_offer,
    "treatment_plan_nudge": treatment_plan_nudge,
}


async def arm_daily(session, ctx) -> None:
    """Clinic jobs that are not part of the shared daily set."""
    from arix.core.timeutil import local_date, now_utc
    from arix.scheduler.jobs import arm_local

    today = local_date(now_utc(), ctx.tz_name)
    if ctx.settings.features.recall and today.day == 1:
        await arm_local(session, ctx, "dormant_invite", "11:00")
    if ctx.settings.features.treatment_plans:
        await arm_local(session, ctx, "treatment_plan_nudge", "10:30")


def register() -> None:
    """Wire the clinic module into the shared scheduler."""
    from arix.scheduler.registry import (
        register_daily,
        register_handlers,
        register_report_builder,
        register_slot_hint,
    )

    register_handlers(CLINIC_HANDLERS)
    register_daily(arm_daily)
    register_slot_hint(slot_hint)
    register_report_builder(lambda session, ctx, day: build_daily_report(session, ctx, day))


__all__ = ["CLINIC_HANDLERS", "register", "schedule_noshow_rebook"]
