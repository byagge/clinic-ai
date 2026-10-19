"""Outbound delivery.

One function sends every patient-facing message, so counters, quiet hours,
opt-outs, the WhatsApp 24h window and channel-failure alerts are enforced in
exactly one place.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from arix.channels.base import Button, DeliveryResult, OutboundMessage
from arix.channels.registry import build_adapter, get_channel
from arix.config import get_settings
from arix.core.context import TenantContext
from arix.core.events import DELIVERY_FAILED, OUTBOUND, log_event
from arix.core.timeutil import month_period, now_utc
from arix.db.models_core import Channel, Contact, Conversation, OutboundCounter
from arix.logging_setup import get_logger

from .conversations import store_outbound

log = get_logger("arix.messaging")


@dataclass
class SendOutcome:
    ok: bool
    message_id: uuid.UUID | None = None
    error: str | None = None
    skipped_reason: str | None = None


async def send_message(
    session: AsyncSession,
    ctx: TenantContext,
    conversation: Conversation,
    text: str,
    *,
    buttons: list[Button] | None = None,
    sender: str = "bot",
    template: dict | None = None,
    is_marketing: bool = False,
    contact: Contact | None = None,
) -> SendOutcome:
    if not text or not text.strip():
        return SendOutcome(ok=False, skipped_reason="empty")

    contact = contact or (
        await session.execute(select(Contact).where(Contact.id == conversation.contact_id))
    ).scalar_one_or_none()
    if contact is not None:
        if contact.anonymized_at is not None:
            return SendOutcome(ok=False, skipped_reason="anonymized")
        if is_marketing and not contact.marketing_allowed:
            return SendOutcome(ok=False, skipped_reason="no_marketing_consent")

    channel = (
        await session.execute(select(Channel).where(Channel.id == conversation.channel_id))
    ).scalar_one_or_none() if conversation.channel_id else None
    if channel is None:
        channel = await get_channel(session, ctx.id, conversation.channel_type)
    if channel is None:
        return SendOutcome(ok=False, skipped_reason="no_channel")

    # Cloud API: outside the 24h window only an approved template may be sent.
    needs_template = (
        channel.type == "wa"
        and (channel.settings or {}).get("mode") == "cloud_api"
        and (
            conversation.window_expires_at is None
            or conversation.window_expires_at <= now_utc()
        )
    )
    if needs_template and template is None:
        log.warning("outside_wa_window", tenant=str(ctx.id), conversation=str(conversation.id))
        return SendOutcome(ok=False, skipped_reason="outside_window_no_template")

    adapter = build_adapter(channel)
    result: DeliveryResult = await adapter.send(
        OutboundMessage(
            peer_id=conversation.peer_id,
            text=text,
            buttons=buttons or [],
            business_connection_id=conversation.business_connection_id,
            template=template,
        )
    )

    message = await store_outbound(
        session,
        ctx,
        conversation,
        text,
        external_id=result.external_id,
        sender=sender,
        meta={
            "buttons": [b.text for b in (buttons or [])],
            "delivered": result.ok,
            "error": result.error,
            "template": bool(template),
        },
    )

    if result.ok:
        await _bump_counter(session, ctx.id, channel.id, billable=result.billable)
        await log_event(
            session,
            ctx.id,
            OUTBOUND,
            contact_id=conversation.contact_id,
            conversation_id=conversation.id,
            source=sender,
            data={"channel": channel.type, "billable": result.billable, "chars": len(text)},
        )
        return SendOutcome(ok=True, message_id=message.id)

    await log_event(
        session,
        ctx.id,
        DELIVERY_FAILED,
        contact_id=conversation.contact_id,
        conversation_id=conversation.id,
        data={"channel": channel.type, "error": result.error},
    )
    if result.channel_broken:
        await session.execute(
            update(Channel)
            .where(Channel.id == channel.id)
            .values(status="error", status_detail=(result.error or "")[:500])
        )
        from arix.ops import alert_ops

        await alert_ops(
            f"⚠️ Канал {channel.type} у «{ctx.name}» отвалился: {result.error}",
        )
    return SendOutcome(ok=False, error=result.error, message_id=message.id)


async def _bump_counter(
    session: AsyncSession, tenant_id: uuid.UUID, channel_id: uuid.UUID, *, billable: bool
) -> None:
    period = month_period(now_utc())
    stmt = (
        pg_insert(OutboundCounter)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            channel_id=channel_id,
            period=period,
            sent=1,
            billable=1 if billable else 0,
        )
        .on_conflict_do_update(
            index_elements=[
                OutboundCounter.tenant_id,
                OutboundCounter.channel_id,
                OutboundCounter.period,
            ],
            set_={
                "sent": OutboundCounter.sent + 1,
                "billable": OutboundCounter.billable + (1 if billable else 0),
            },
        )
        .returning(OutboundCounter.billable, OutboundCounter.alerted_at)
    )
    row = (await session.execute(stmt)).first()
    if not row:
        return
    billable_count, alerted_at = row
    threshold = int(get_settings().wa_free_tier_monthly * 0.8)
    if billable and billable_count >= threshold and alerted_at is None:
        await session.execute(
            update(OutboundCounter)
            .where(
                OutboundCounter.tenant_id == tenant_id,
                OutboundCounter.channel_id == channel_id,
                OutboundCounter.period == period,
            )
            .values(alerted_at=now_utc())
        )
        from arix.ops import alert_ops

        await alert_ops(
            f"📊 WhatsApp: тенант {tenant_id} израсходовал {billable_count} платных сообщений "
            f"за {period} (порог {threshold})."
        )


__all__ = ["SendOutcome", "send_message"]
