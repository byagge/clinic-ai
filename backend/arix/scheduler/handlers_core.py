"""Job handlers shared by every vertical."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.events import FOLLOWUP_SENT, log_event
from arix.core.timeutil import now_utc
from arix.db.models_core import Contact, Conversation, Handoff, ScheduledJob, Tenant
from arix.domain.handoff import handoff_card, resolve_handoff
from arix.domain.messaging import send_message
from arix.domain.notify import notify_staff
from arix.logging_setup import get_logger
from arix.templates import render

log = get_logger("arix.jobs")

HandlerResult = dict


async def _conversation(
    session: AsyncSession, ctx: TenantContext, conversation_id: str
) -> tuple[Conversation, Contact] | None:
    conversation = (
        await session.execute(
            select(Conversation).where(
                Conversation.tenant_id == ctx.id, Conversation.id == uuid.UUID(conversation_id)
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        return None
    contact = (
        await session.execute(select(Contact).where(Contact.id == conversation.contact_id))
    ).scalar_one_or_none()
    if contact is None:
        return None
    return conversation, contact


async def handoff_pickup_check(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> HandlerResult:
    """Nobody took the handoff in time → remind the admins."""
    handoff = await _handoff(session, ctx, job)
    if handoff is None or handoff.taken_at or handoff.resolved_at:
        return {"skipped": "already_handled"}
    text, buttons = await handoff_card(session, ctx, handoff)
    sent = await notify_staff(
        session, ctx, roles=["admin"],
        text=f"⏰ Передача #{handoff.number} ждёт {ctx.settings.handoff.pickup_minutes} мин\n\n{text}",
        buttons=buttons,
    )
    return {"reminded": sent}


async def handoff_escalate(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> HandlerResult:
    handoff = await _handoff(session, ctx, job)
    if handoff is None or handoff.taken_at or handoff.resolved_at:
        return {"skipped": "already_handled"}
    text, buttons = await handoff_card(session, ctx, handoff)
    handoff.escalated_at = now_utc()
    sent = await notify_staff(
        session, ctx, roles=["owner"],
        text=f"🔺 Передачу #{handoff.number} никто не взял за "
        f"{ctx.settings.handoff.escalate_minutes} мин\n\n{text}",
        buttons=buttons,
    )
    return {"escalated": sent}


async def night_handoff_digest(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> HandlerResult:
    """09:00 batch of everything that came in during quiet hours (ТЗ §7 С12)."""
    rows = list(
        (
            await session.execute(
                select(Handoff).where(
                    Handoff.tenant_id == ctx.id,
                    Handoff.resolved_at.is_(None),
                    Handoff.queued_until.isnot(None),
                )
                .order_by(Handoff.created_at)
            )
        ).scalars()
    )
    if not rows:
        return {"queued": 0}
    blocks = []
    for handoff in rows:
        text, _buttons = await handoff_card(session, ctx, handoff)
        blocks.append(text)
        handoff.queued_until = None
        handoff.notified_at = now_utc()
    await notify_staff(
        session, ctx, roles=["admin", "owner"],
        text=f"🌙 За ночь накопилось передач: {len(rows)}\n\n" + "\n\n".join(blocks[:10]),
    )
    await session.flush()
    return {"queued": len(rows)}


async def followup(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> HandlerResult:
    """Nudge an unconverted interest (ТЗ §7 С6)."""
    payload = job.payload or {}
    pair = await _conversation(session, ctx, payload["conversation_id"])
    if pair is None:
        return {"skipped": "no_conversation"}
    conversation, contact = pair

    if contact.opted_out_at is not None:
        return {"skipped": "opted_out"}
    if conversation.assigned_to != "bot":
        return {"skipped": "with_human"}
    # Stop condition: the patient replied after the interest was recorded.
    if (
        conversation.last_inbound_at
        and conversation.last_outbound_at
        and conversation.last_inbound_at > conversation.last_outbound_at
    ):
        return {"skipped": "patient_replied"}

    step = int(payload.get("step") or 1)
    code = f"followup_{min(step, 3)}"
    service = payload.get("service") or "услугу"
    slots_text = await _slots_hint(session, ctx, payload)
    if step >= 3 and not slots_text:
        return {"skipped": "nothing_to_offer"}

    text = render(
        code, contact.lang, overrides=ctx.settings.texts,
        service=service, slots=slots_text or "свободное время",
        reason="на этой неделе есть свободные окна." if step >= 3 else "",
    )
    outcome = await send_message(session, ctx, conversation, text, contact=contact)
    if outcome.ok:
        await log_event(
            session, ctx.id, FOLLOWUP_SENT, contact_id=contact.id,
            conversation_id=conversation.id, source="followup", data={"step": step},
        )
    return {"sent": outcome.ok, "step": step, "reason": outcome.skipped_reason}


async def _slots_hint(session: AsyncSession, ctx: TenantContext, payload: dict) -> str | None:
    """Ask the vertical for 2 concrete times to put in the follow-up text."""
    from arix.scheduler.registry import slot_hint_provider

    provider = slot_hint_provider()
    if provider is None:
        return None
    return await provider(session, ctx, payload)


async def demo_expire(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> HandlerResult:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == ctx.id))
    ).scalar_one()
    if tenant.status != "demo":
        return {"skipped": "not_demo"}
    tenant.status = "archived"
    await session.flush()
    return {"archived": True}


async def metrics_rollup(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> HandlerResult:
    from arix.scheduler.registry import report_builder

    builder = report_builder()
    if builder is None:
        return {"skipped": "no_builder"}
    report = await builder(session, ctx, None)
    return {"day": str(getattr(report, "day", ""))}


async def resolve_stale_handoffs(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> HandlerResult:
    """Close handoffs a human picked up but never closed (hygiene)."""
    cutoff = now_utc() - timedelta(hours=24)
    rows = list(
        (
            await session.execute(
                select(Handoff).where(
                    Handoff.tenant_id == ctx.id,
                    Handoff.resolved_at.is_(None),
                    Handoff.taken_at.isnot(None),
                    Handoff.taken_at < cutoff,
                )
            )
        ).scalars()
    )
    for handoff in rows:
        await resolve_handoff(session, ctx, handoff.id, back_to_bot=True)
    return {"closed": len(rows)}


async def wa_quota_check(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> HandlerResult:
    """Report WhatsApp spend to the owner once a month."""
    from sqlalchemy import func

    from arix.core.timeutil import month_period
    from arix.db.models_core import OutboundCounter

    period = month_period(now_utc())
    total = (
        await session.execute(
            select(func.sum(OutboundCounter.sent), func.sum(OutboundCounter.billable)).where(
                OutboundCounter.tenant_id == ctx.id, OutboundCounter.period == period
            )
        )
    ).first()
    sent, billable = (total or (0, 0))
    if not sent:
        return {"sent": 0}
    await notify_staff(
        session, ctx, roles=["owner"],
        text=f"📨 {period}: отправлено {sent} сообщений, из них платных {billable or 0}.",
    )
    return {"sent": int(sent), "billable": int(billable or 0)}


async def _handoff(
    session: AsyncSession, ctx: TenantContext, job: ScheduledJob
) -> Handoff | None:
    raw = (job.payload or {}).get("handoff_id")
    if not raw:
        return None
    return (
        await session.execute(
            select(Handoff).where(Handoff.tenant_id == ctx.id, Handoff.id == uuid.UUID(raw))
        )
    ).scalar_one_or_none()


CORE_HANDLERS = {
    "handoff_pickup_check": handoff_pickup_check,
    "handoff_escalate": handoff_escalate,
    "night_handoff_digest": night_handoff_digest,
    "followup": followup,
    "demo_expire": demo_expire,
    "metrics_rollup": metrics_rollup,
    "resolve_stale_handoffs": resolve_stale_handoffs,
    "wa_quota_check": wa_quota_check,
}

__all__ = ["CORE_HANDLERS"]
