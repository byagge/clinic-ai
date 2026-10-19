"""Handoff to a human (ТЗ §6.3).

Flow: create the handoff → notify admins in Telegram with a card and buttons →
if nobody takes it within ``pickup_minutes``, remind; after
``escalate_minutes``, page the owner.  At night the handoff is queued and
delivered with the 09:00 batch instead of waking anyone.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.events import HANDOFF_CREATED, log_event
from arix.core.timeutil import now_utc, to_tenant
from arix.db.models_core import Contact, Conversation, Handoff, Staff
from arix.scheduler import jobs

URGENT = ("red_flag",)


async def create_handoff(
    session: AsyncSession,
    ctx: TenantContext,
    conversation: Conversation,
    *,
    reason: str,
    priority: str = "normal",
    summary: str | None = None,
) -> Handoff:
    """Create (or reuse) an open handoff for this conversation."""
    existing = (
        await session.execute(
            select(Handoff).where(
                Handoff.tenant_id == ctx.id,
                Handoff.conversation_id == conversation.id,
                Handoff.resolved_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Escalate an existing handoff rather than stacking duplicates.
        if priority == "urgent" and existing.priority != "urgent":
            existing.priority = "urgent"
            existing.summary = summary or existing.summary
            await session.flush()
        return existing

    next_number = (
        await session.execute(
            select(func.coalesce(func.max(Handoff.number), 0) + 1).where(
                Handoff.tenant_id == ctx.id
            )
        )
    ).scalar_one()

    quiet = ctx.settings.quiet
    local_now = to_tenant(now_utc(), ctx.tz_name)
    night = bool(quiet and quiet.contains_local(local_now)) and priority != "urgent"

    handoff = Handoff(
        tenant_id=ctx.id,
        conversation_id=conversation.id,
        number=next_number,
        reason=reason,
        priority=priority,
        summary=(summary or "")[:2000] or None,
        queued_until=quiet.next_allowed_local(local_now).astimezone(local_now.tzinfo)
        if night and quiet
        else None,
    )
    session.add(handoff)
    conversation.state = "awaiting_human"
    await session.flush()

    await log_event(
        session,
        ctx.id,
        HANDOFF_CREATED,
        contact_id=conversation.contact_id,
        conversation_id=conversation.id,
        entity_ref=str(handoff.id),
        data={"reason": reason, "priority": priority, "night": night},
    )

    cfg = ctx.settings.handoff
    if not night:
        await jobs.enqueue(
            session,
            ctx.id,
            "handoff_pickup_check",
            now_utc() + _minutes(cfg.pickup_minutes),
            dedupe_key=jobs.dedupe("handoff_pickup", handoff.id),
            payload={"handoff_id": str(handoff.id)},
            timezone=ctx.tz_name,
        )
        await jobs.enqueue(
            session,
            ctx.id,
            "handoff_escalate",
            now_utc() + _minutes(cfg.escalate_minutes),
            dedupe_key=jobs.dedupe("handoff_escalate", handoff.id),
            payload={"handoff_id": str(handoff.id)},
            timezone=ctx.tz_name,
        )
    return handoff


def _minutes(value: int):
    from datetime import timedelta

    return timedelta(minutes=value)


async def take_handoff(
    session: AsyncSession, ctx: TenantContext, handoff_id: uuid.UUID, staff_id: uuid.UUID
) -> Handoff | None:
    handoff = (
        await session.execute(
            select(Handoff).where(Handoff.tenant_id == ctx.id, Handoff.id == handoff_id)
        )
    ).scalar_one_or_none()
    if handoff is None or handoff.taken_at is not None:
        return handoff
    handoff.taken_by = staff_id
    handoff.taken_at = now_utc()
    conversation = (
        await session.execute(
            select(Conversation).where(Conversation.id == handoff.conversation_id)
        )
    ).scalar_one()
    conversation.assigned_to = str(staff_id)
    conversation.state = "with_human"
    await session.flush()
    await jobs.cancel(
        session,
        ctx.id,
        dedupe_prefix=f"handoff_escalate:{handoff.id}",
    )
    await log_event(
        session,
        ctx.id,
        "handoff_taken",
        conversation_id=conversation.id,
        entity_ref=str(handoff.id),
        data={"staff_id": str(staff_id)},
    )
    return handoff


async def resolve_handoff(
    session: AsyncSession, ctx: TenantContext, handoff_id: uuid.UUID, *, back_to_bot: bool = True
) -> None:
    handoff = (
        await session.execute(
            select(Handoff).where(Handoff.tenant_id == ctx.id, Handoff.id == handoff_id)
        )
    ).scalar_one_or_none()
    if handoff is None:
        return
    handoff.resolved_at = now_utc()
    if back_to_bot:
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.id == handoff.conversation_id)
            )
        ).scalar_one()
        conversation.assigned_to = "bot"
        conversation.state = "active"
    await session.flush()


async def open_handoffs(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int = 50
) -> list[Handoff]:
    return list(
        (
            await session.execute(
                select(Handoff)
                .where(Handoff.tenant_id == tenant_id, Handoff.resolved_at.is_(None))
                .order_by(Handoff.priority.desc(), Handoff.created_at)
                .limit(limit)
            )
        ).scalars()
    )


async def staff_for_role(
    session: AsyncSession, tenant_id: uuid.UUID, roles: list[str]
) -> list[Staff]:
    return list(
        (
            await session.execute(
                select(Staff).where(
                    Staff.tenant_id == tenant_id,
                    Staff.role.in_(roles),
                    Staff.active.is_(True),
                    Staff.tg_user_id.isnot(None),
                )
            )
        ).scalars()
    )


async def handoff_card(
    session: AsyncSession, ctx: TenantContext, handoff: Handoff
) -> tuple[str, list[tuple[str, str]]]:
    """Render the admin card text and its buttons."""
    conversation = (
        await session.execute(
            select(Conversation).where(Conversation.id == handoff.conversation_id)
        )
    ).scalar_one()
    contact = (
        await session.execute(select(Contact).where(Contact.id == conversation.contact_id))
    ).scalar_one()
    reason_ru = {
        "patient_request": "просит человека",
        "red_flag": "🚨 красный флаг",
        "complaint": "жалоба",
        "discount": "торг/скидка",
        "refund": "возврат денег",
        "out_of_kb": "вопрос вне базы",
        "media": "фото/документ",
        "not_online_bookable": "запись только через админа",
        "confusion": "бот не понял",
        "other": "другое",
    }.get(handoff.reason, handoff.reason)

    mark = {"urgent": "🔴", "high": "🟠", "normal": "🟡"}.get(handoff.priority, "🟡")
    lines = [
        f"{mark} Передача #{handoff.number} · {reason_ru}",
        f"Клиент: {contact.name or 'без имени'}"
        + (f" · {contact.phone}" if contact.phone else ""),
        f"Канал: {conversation.channel_type.upper()}",
    ]
    if handoff.summary:
        lines.append(f"Суть: {handoff.summary}")
    lines.append(f"Время: {to_tenant(handoff.created_at, ctx.tz_name):%H:%M}")
    buttons = [
        ("Беру", f"ho:take:{handoff.id}"),
        ("Вернуть боту", f"ho:bot:{handoff.id}"),
        ("Диалог", f"ho:open:{handoff.id}"),
    ]
    return "\n".join(lines), buttons


__all__ = [
    "create_handoff",
    "handoff_card",
    "open_handoffs",
    "resolve_handoff",
    "staff_for_role",
    "take_handoff",
]
