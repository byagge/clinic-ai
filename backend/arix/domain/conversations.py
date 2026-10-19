"""Contacts, conversations and message history."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arix.channels.base import InboundMessage
from arix.core.context import TenantContext
from arix.core.pii import normalize_phone
from arix.core.timeutil import now_utc
from arix.db.models_core import Contact, ContactIdentity, Conversation, Message

#: How many recent turns go into the prompt before we rely on the summary.
HISTORY_TURNS = 16


async def resolve_contact(
    session: AsyncSession, ctx: TenantContext, inbound: InboundMessage
) -> Contact:
    """Find or create the contact behind an inbound message.

    Identity resolution order: channel identity → phone → new contact.  The
    identity row is what keeps a Telegram user and a WhatsApp number from
    becoming two patients when both map to the same phone.
    """
    identity = (
        await session.execute(
            select(ContactIdentity).where(
                ContactIdentity.tenant_id == ctx.id,
                ContactIdentity.channel_type == inbound.channel_type,
                ContactIdentity.external_id == inbound.peer_id,
            )
        )
    ).scalar_one_or_none()
    if identity is not None:
        contact = (
            await session.execute(select(Contact).where(Contact.id == identity.contact_id))
        ).scalar_one_or_none()
        if contact is not None:
            _enrich(contact, inbound, ctx)
            return contact

    phone = normalize_phone(inbound.phone, ctx.tenant.country) if inbound.phone else None
    contact: Contact | None = None
    if phone:
        contact = (
            await session.execute(
                select(Contact).where(Contact.tenant_id == ctx.id, Contact.phone == phone)
            )
        ).scalar_one_or_none()

    if contact is None:
        contact = Contact(
            tenant_id=ctx.id,
            phone=phone,
            name=inbound.sender_name,
            lang=ctx.default_lang,
            source="organic",
        )
        try:
            async with session.begin_nested():
                session.add(contact)
                await session.flush()
        except IntegrityError:
            # Two webhooks for a brand-new patient raced; the other won.
            contact = (
                await session.execute(
                    select(Contact).where(Contact.tenant_id == ctx.id, Contact.phone == phone)
                )
            ).scalar_one()

    await session.execute(
        pg_insert(ContactIdentity)
        .values(
            id=uuid.uuid4(),
            tenant_id=ctx.id,
            contact_id=contact.id,
            channel_type=inbound.channel_type,
            external_id=inbound.peer_id,
        )
        .on_conflict_do_nothing(
            index_elements=[
                ContactIdentity.tenant_id,
                ContactIdentity.channel_type,
                ContactIdentity.external_id,
            ]
        )
    )
    _enrich(contact, inbound, ctx)
    return contact


def _enrich(contact: Contact, inbound: InboundMessage, ctx: TenantContext) -> None:
    if not contact.name and inbound.sender_name:
        contact.name = inbound.sender_name[:160]
    if not contact.phone and inbound.phone:
        contact.phone = normalize_phone(inbound.phone, ctx.tenant.country)


async def resolve_conversation(
    session: AsyncSession, ctx: TenantContext, contact: Contact, inbound: InboundMessage
) -> Conversation:
    conversation = (
        await session.execute(
            select(Conversation).where(
                Conversation.tenant_id == ctx.id,
                Conversation.channel_type == inbound.channel_type,
                Conversation.peer_id == inbound.peer_id,
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        from arix.channels.registry import get_channel

        channel = await get_channel(session, ctx.id, inbound.channel_type)
        conversation = Conversation(
            tenant_id=ctx.id,
            contact_id=contact.id,
            channel_id=channel.id if channel else None,
            channel_type=inbound.channel_type,
            peer_id=inbound.peer_id,
            business_connection_id=inbound.business_connection_id,
            state="new",
            assigned_to="bot",
        )
        session.add(conversation)
        await session.flush()
    elif inbound.business_connection_id and not conversation.business_connection_id:
        conversation.business_connection_id = inbound.business_connection_id
    return conversation


async def store_inbound(
    session: AsyncSession,
    ctx: TenantContext,
    conversation: Conversation,
    inbound: InboundMessage,
    *,
    stt_text: str | None = None,
) -> Message | None:
    """Persist an inbound message. Returns ``None`` if it is a replay."""
    existing = (
        await session.execute(
            select(Message.id).where(
                Message.tenant_id == ctx.id, Message.external_id == inbound.external_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    message = Message(
        tenant_id=ctx.id,
        conversation_id=conversation.id,
        direction="in",
        type=inbound.type,
        text=inbound.text or inbound.callback_data,
        stt_text=stt_text,
        media_url=inbound.media_url,
        external_id=inbound.external_id,
        sender="business" if inbound.from_business else "contact",
        meta={
            "callback_data": inbound.callback_data,
            "from_user_id": inbound.from_user_id,
            "mime": inbound.media_mime,
        },
    )
    session.add(message)
    conversation.last_inbound_at = now_utc()
    if conversation.state == "new":
        conversation.state = "active"
    # WhatsApp Cloud API: a patient message opens a 24h free-form window.
    if conversation.channel_type == "wa":
        conversation.window_expires_at = now_utc() + timedelta(hours=24)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        # Concurrent delivery of the same provider message id.
        return None
    return message


async def store_outbound(
    session: AsyncSession,
    ctx: TenantContext,
    conversation: Conversation,
    text: str,
    *,
    external_id: str | None = None,
    sender: str = "bot",
    meta: dict | None = None,
) -> Message:
    message = Message(
        tenant_id=ctx.id,
        conversation_id=conversation.id,
        direction="out",
        type="text",
        text=text,
        external_id=external_id,
        sender=sender,
        meta=meta or {},
    )
    session.add(message)
    conversation.last_outbound_at = now_utc()
    await session.flush()
    return message


async def history(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    limit: int = HISTORY_TURNS,
) -> list[Message]:
    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(desc(Message.seq))
            .limit(limit)
        )
    ).scalars()
    return list(reversed(list(rows)))


def to_llm_messages(messages: list[Message], mask) -> list[dict]:
    """Render stored history as Anthropic messages, PII-masked.

    ``mask`` is a callable returning the masked text; the vault it writes into
    is owned by the caller so the reply can be unmasked afterwards.
    """
    out: list[dict] = []
    for message in messages:
        text = message.stt_text or message.text
        if not text:
            text = {"image": "[фото]", "document": "[документ]", "voice": "[голосовое]"}.get(
                message.type, ""
            )
        if not text:
            continue
        role = "user" if message.direction == "in" else "assistant"
        content = mask(text)
        if out and out[-1]["role"] == role:
            out[-1]["content"] = f"{out[-1]['content']}\n{content}"
        else:
            out.append({"role": role, "content": content})
    # Anthropic requires the conversation to start with a user turn.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


async def assign_to_human(
    session: AsyncSession, conversation: Conversation, staff_id: uuid.UUID | None
) -> None:
    conversation.assigned_to = str(staff_id) if staff_id else "human"
    conversation.state = "with_human"
    await session.flush()


async def return_to_bot(session: AsyncSession, conversation: Conversation) -> None:
    conversation.assigned_to = "bot"
    conversation.state = "active"
    await session.flush()


__all__ = [
    "HISTORY_TURNS",
    "assign_to_human",
    "history",
    "resolve_contact",
    "resolve_conversation",
    "return_to_bot",
    "store_inbound",
    "store_outbound",
    "to_llm_messages",
]
