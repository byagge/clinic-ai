"""Resolve a stored ``channels`` row into a live adapter."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.config import get_settings
from arix.core.crypto import decrypt_json
from arix.db.models_core import Channel

from .base import ChannelAdapter, ChannelError
from .telegram import TelegramAdapter
from .whatsapp import BaileysAdapter, CloudApiAdapter


def build_adapter(channel: Channel) -> ChannelAdapter:
    s = get_settings()
    if s.channels_dry_run or (channel.settings or {}).get("dry_run"):
        from .dryrun import DryRunAdapter

        return DryRunAdapter(f"{channel.type}:{channel.tenant_id}")
    creds = decrypt_json(channel.credentials_enc)
    if channel.type in ("tg", "tg_business"):
        token = creds.get("bot_token") or s.telegram_shared_bot_token
        if not token:
            raise ChannelError("telegram token is not configured")
        return TelegramAdapter(token)
    if channel.type == "wa":
        mode = (channel.settings or {}).get("mode") or s.wa_provider
        if mode == "cloud_api":
            phone_number_id = creds.get("phone_number_id")
            token = creds.get("access_token") or s.wa_cloud_api_token
            if not (phone_number_id and token):
                raise ChannelError("whatsapp cloud api credentials missing")
            return CloudApiAdapter(phone_number_id, token)
        session_id = (channel.settings or {}).get("session_id") or str(channel.tenant_id)
        return BaileysAdapter(session_id)
    raise ChannelError(f"unsupported channel type {channel.type}")


async def get_channel(
    session: AsyncSession, tenant_id: uuid.UUID, channel_type: str
) -> Channel | None:
    want = ["tg", "tg_business"] if channel_type in ("tg", "tg_business") else [channel_type]
    return (
        await session.execute(
            select(Channel)
            .where(
                Channel.tenant_id == tenant_id,
                Channel.type.in_(want),
                Channel.status != "disabled",
            )
            .order_by(Channel.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def adapter_for(
    session: AsyncSession, tenant_id: uuid.UUID, channel_type: str
) -> tuple[Channel, ChannelAdapter]:
    channel = await get_channel(session, tenant_id, channel_type)
    if channel is None:
        raise ChannelError(f"tenant has no {channel_type} channel")
    return channel, build_adapter(channel)


__all__ = ["adapter_for", "build_adapter", "get_channel"]
