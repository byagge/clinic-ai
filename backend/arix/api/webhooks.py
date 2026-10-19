"""Inbound webhooks: Telegram (bot + business), WhatsApp (Baileys / Cloud API).

Every endpoint is: verify → parse → dedupe → hand to the runtime → 200.
We always answer 200 once the message is persisted, because providers retry on
anything else and a retry storm is worse than a dropped reply we can re-drive
from ``messages``.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response, status
from sqlalchemy import select

from arix.channels.base import InboundMessage
from arix.channels.telegram import (
    TelegramAdapter,
    parse_business_connection,
    parse_update,
)
from arix.channels.whatsapp import parse_baileys_webhook, parse_cloud_webhook
from arix.config import get_settings
from arix.core.context import load_context
from arix.core.crypto import decrypt_json, verify_signature
from arix.db.models_core import Channel, Tenant
from arix.db.session import admin_session, tenant_session
from arix.logging_setup import bind_context, get_logger
from arix.verticals.clinic.adapter import build_runtime

log = get_logger("arix.webhooks")
router = APIRouter(tags=["webhooks"])

_runtime = None


def runtime():
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
    return _runtime


async def _tenant_by_slug(slug: str) -> uuid.UUID:
    async with admin_session() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.slug == slug))
        ).scalar_one_or_none()
    if tenant_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown tenant")
    return tenant_id


async def _dispatch(tenant_id: uuid.UUID, inbound: InboundMessage) -> None:
    """Run one inbound message through the engine in its own transaction."""
    bind_context(tenant_id=str(tenant_id), channel=inbound.channel_type)
    try:
        async with tenant_session(tenant_id) as session:
            ctx = await load_context(session, tenant_id)

            # Staff talk to the bot in their own private chat; their messages
            # are commands, not patient dialogue.
            if inbound.channel_type == "tg":
                from arix.bots.staff import handle_staff_update

                if await handle_staff_update(session, ctx, inbound):
                    log.info("staff_command", text=(inbound.text or inbound.callback_data))
                    return

            # /pause_bot: store the message, let the humans answer.
            if ctx.settings.agent.paused:
                from arix.domain import conversations as convo
                from arix.domain.notify import notify_staff

                contact = await convo.resolve_contact(session, ctx, inbound)
                conversation = await convo.resolve_conversation(session, ctx, contact, inbound)
                await convo.store_inbound(session, ctx, conversation, inbound)
                await notify_staff(
                    session, ctx, roles=["admin", "owner"],
                    text=f"⏸ Бот на паузе. Сообщение от {contact.name or conversation.peer_id}:\n"
                    f"{(inbound.text or '')[:500]}",
                )
                log.info("bot_paused_stored")
                return

            result = await runtime().handle(session, ctx, inbound)
            log.info(
                "turn",
                handled=result.handled,
                tools=result.tool_calls,
                skipped=result.skipped_reason,
                violations=result.guard_violations,
            )
    except Exception as exc:  # never let a webhook crash the process
        log.exception("dispatch_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


@router.post("/tg/{slug}")
async def telegram_webhook(
    slug: str,
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> Response:
    s = get_settings()
    if s.telegram_webhook_secret and x_telegram_bot_api_secret_token != s.telegram_webhook_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook secret")

    tenant_id = await _tenant_by_slug(slug)
    update = await request.json()

    connection = parse_business_connection(update)
    if connection is not None:
        await _store_business_connection(tenant_id, connection)
        return Response(status_code=200)

    inbound = parse_update(update)
    if inbound is None:
        return Response(status_code=200)

    # Clear the button spinner immediately; the reply follows asynchronously.
    if inbound.callback_id:
        background.add_task(_answer_callback, tenant_id, inbound.callback_id)
    if inbound.type == "voice" and inbound.media_url:
        background.add_task(_dispatch_voice, tenant_id, inbound)
    else:
        background.add_task(_dispatch, tenant_id, inbound)
    return Response(status_code=200)


async def _answer_callback(tenant_id: uuid.UUID, callback_id: str) -> None:
    try:
        async with tenant_session(tenant_id) as session:
            from arix.channels.registry import adapter_for

            _channel, adapter = await adapter_for(session, tenant_id, "tg")
            if isinstance(adapter, TelegramAdapter):
                await adapter.answer_callback(callback_id)
    except Exception as exc:  # pragma: no cover - cosmetic
        log.warning("answer_callback_failed", error=str(exc))


async def _dispatch_voice(tenant_id: uuid.UUID, inbound: InboundMessage) -> None:
    """Download the voice note before the runtime transcribes it."""
    try:
        async with tenant_session(tenant_id) as session:
            from arix.channels.registry import adapter_for

            _channel, adapter = await adapter_for(session, tenant_id, "tg")
            if isinstance(adapter, TelegramAdapter) and inbound.media_url:
                data, mime = await adapter.file_bytes(inbound.media_url)
                inbound.media_bytes = data
                inbound.media_mime = mime or inbound.media_mime
    except Exception as exc:
        log.warning("voice_download_failed", error=str(exc))
    await _dispatch(tenant_id, inbound)


async def _store_business_connection(tenant_id: uuid.UUID, connection: dict) -> None:
    """Remember the Telegram Business connection id and the owner's user id."""
    async with tenant_session(tenant_id) as session:
        channel = (
            await session.execute(
                select(Channel).where(
                    Channel.tenant_id == tenant_id, Channel.type.in_(["tg", "tg_business"])
                )
            )
        ).scalars().first()
        if channel is None:
            channel = Channel(
                tenant_id=tenant_id, type="tg_business", status="connected",
                external_id=connection["id"], settings={},
            )
            session.add(channel)
        channel.type = "tg_business"
        channel.external_id = connection["id"]
        channel.status = "connected" if connection["enabled"] else "disabled"
        channel.settings = {
            **(channel.settings or {}),
            "business_connection_id": connection["id"],
            "owner_user_id": connection.get("user_id"),
            "can_reply": connection.get("can_reply", True),
        }
        await session.flush()
    log.info("business_connection", tenant=str(tenant_id), enabled=connection["enabled"])


@router.post("/tg/shared")
async def telegram_shared_webhook(
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> Response:
    """One webhook for the shared ARIX bot that serves demos and small tenants.

    ``/start t_<slug>`` binds the Telegram user to a tenant (``channel_routes``);
    every later message from that user goes to the same tenant until another
    deeplink is opened.
    """
    s = get_settings()
    if s.telegram_webhook_secret and x_telegram_bot_api_secret_token != s.telegram_webhook_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook secret")

    update = await request.json()
    inbound = parse_update(update)
    if inbound is None:
        return Response(status_code=200)

    slug = _deeplink_slug(inbound.text)
    tenant_id = await _route_shared(inbound, slug)
    if tenant_id is None:
        return Response(status_code=200)

    if slug and (inbound.text or "").startswith("/start"):
        # Swallow the /start itself and send the tenant's greeting instead.
        background.add_task(_greet, tenant_id, inbound)
        return Response(status_code=200)

    if inbound.callback_id:
        background.add_task(_answer_callback, tenant_id, inbound.callback_id)
    if inbound.type == "voice" and inbound.media_url:
        background.add_task(_dispatch_voice, tenant_id, inbound)
    else:
        background.add_task(_dispatch, tenant_id, inbound)
    return Response(status_code=200)


def _deeplink_slug(text: str | None) -> str | None:
    if not text or not text.startswith("/start"):
        return None
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    payload = parts[1].strip()
    return payload[2:] if payload.startswith("t_") else None


async def _route_shared(inbound: InboundMessage, slug: str | None) -> uuid.UUID | None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from arix.core.timeutil import now_utc
    from arix.db.models_core import ChannelRoute

    async with admin_session() as session:
        tenant_id = None
        if slug:
            tenant_id = (
                await session.execute(select(Tenant.id).where(Tenant.slug == slug))
            ).scalar_one_or_none()
            if tenant_id is not None:
                await session.execute(
                    pg_insert(ChannelRoute)
                    .values(
                        id=uuid.uuid4(),
                        channel_type=inbound.channel_type,
                        external_id=inbound.peer_id,
                        tenant_id=tenant_id,
                        last_seen_at=now_utc(),
                    )
                    .on_conflict_do_update(
                        index_elements=[ChannelRoute.channel_type, ChannelRoute.external_id],
                        set_={"tenant_id": tenant_id, "last_seen_at": now_utc()},
                    )
                )
                return tenant_id
        route = (
            await session.execute(
                select(ChannelRoute).where(
                    ChannelRoute.channel_type == inbound.channel_type,
                    ChannelRoute.external_id == inbound.peer_id,
                )
            )
        ).scalar_one_or_none()
        if route is None:
            log.info("shared_bot_unrouted", peer=inbound.peer_id)
            return None
        route.last_seen_at = now_utc()
        return route.tenant_id


async def _greet(tenant_id: uuid.UUID, inbound: InboundMessage) -> None:
    """First contact through a deeplink: greet and offer the first step."""
    from arix.domain import conversations as convo
    from arix.domain.messaging import send_message

    try:
        async with tenant_session(tenant_id) as session:
            ctx = await load_context(session, tenant_id)
            contact = await convo.resolve_contact(session, ctx, inbound)
            conversation = await convo.resolve_conversation(session, ctx, contact, inbound)
            greeting = ctx.settings.agent.greeting or (
                f"Здравствуйте! Это {ctx.name}"
                + (f", {ctx.tenant.city}. " if ctx.tenant.city else ". ")
                + "Подскажу цены и свободное время, запишу на приём. Что вас интересует?"
            )
            await send_message(session, ctx, conversation, greeting, contact=contact)
    except Exception as exc:
        log.exception("greet_failed", error=str(exc))


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------


@router.post("/wa/baileys/{slug}")
async def baileys_webhook(
    slug: str,
    request: Request,
    background: BackgroundTasks,
    x_arix_signature: Annotated[str | None, Header()] = None,
) -> Response:
    body = await request.body()
    s = get_settings()
    if not verify_signature(s.wa_webhook_secret, body, x_arix_signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")

    tenant_id = await _tenant_by_slug(slug)
    payload = await request.json()

    event = payload.get("event")
    if event in ("connection.update", "qr", "logout"):
        await _store_wa_status(tenant_id, payload)
        return Response(status_code=200)

    for inbound in parse_baileys_webhook(payload):
        background.add_task(_dispatch, tenant_id, inbound)
    return Response(status_code=200)


async def _store_wa_status(tenant_id: uuid.UUID, payload: dict) -> None:
    status_map = {
        "open": "connected",
        "connecting": "connecting",
        "close": "error",
        "qr": "connecting",
        "logout": "error",
    }
    async with tenant_session(tenant_id) as session:
        channel = (
            await session.execute(
                select(Channel).where(Channel.tenant_id == tenant_id, Channel.type == "wa")
            )
        ).scalars().first()
        if channel is None:
            return
        raw_state = payload.get("state") or payload.get("event")
        channel.status = status_map.get(str(raw_state), channel.status)
        channel.status_detail = str(payload.get("reason") or "")[:500] or None
        channel.settings = {
            **(channel.settings or {}),
            "qr": payload.get("qr"),
            "phone": payload.get("phone"),
        }
        from arix.core.timeutil import now_utc

        channel.last_seen_at = now_utc()
        await session.flush()


@router.get("/wa/cloud/{slug}")
async def cloud_verify(slug: str, request: Request) -> Response:
    """Meta webhook verification handshake."""
    params = request.query_params
    async with admin_session() as session:
        channel = (
            await session.execute(
                select(Channel)
                .join(Tenant, Tenant.id == Channel.tenant_id)
                .where(Tenant.slug == slug, Channel.type == "wa")
            )
        ).scalars().first()
    expected = (decrypt_json(channel.credentials_enc).get("verify_token") if channel else None)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == expected:
        return Response(content=params.get("hub.challenge", ""), status_code=200)
    raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")


@router.post("/wa/cloud/{slug}")
async def cloud_webhook(
    slug: str,
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> Response:
    body = await request.body()
    tenant_id = await _tenant_by_slug(slug)
    async with admin_session() as session:
        channel = (
            await session.execute(
                select(Channel).where(Channel.tenant_id == tenant_id, Channel.type == "wa")
            )
        ).scalars().first()
    secret = (decrypt_json(channel.credentials_enc).get("app_secret") if channel else None)
    if secret and not verify_signature(secret, body, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")

    for inbound in parse_cloud_webhook(await request.json()):
        background.add_task(_dispatch, tenant_id, inbound)
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


@router.post("/pay/{provider}/{slug}")
async def payment_webhook(provider: str, slug: str, request: Request) -> dict:
    payload = await request.json() if request.headers.get("content-type", "").startswith(
        "application/json"
    ) else dict(await request.form())
    tenant_id = await _tenant_by_slug(slug)
    from arix.payments import mark_paid, provider_config, verify_click_signature

    order_id = (
        payload.get("merchant_trans_id")
        or payload.get("order_id")
        or (payload.get("params") or {}).get("account", {}).get("order_id")
    )
    if not order_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no order reference")

    async with tenant_session(tenant_id) as session:
        ctx = await load_context(session, tenant_id)
        config = await provider_config(session, ctx)
        if provider == "click" and config.secret:
            if not verify_click_signature(payload, config.secret):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")
        payment = await mark_paid(
            session, ctx, uuid.UUID(str(order_id)),
            provider_ref=str(payload.get("click_trans_id") or payload.get("transaction") or ""),
        )
        if payment is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown payment")
    return {"ok": True, "error": 0, "error_note": "Success"}


__all__ = ["router"]
