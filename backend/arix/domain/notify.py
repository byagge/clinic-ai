"""Staff notifications over Telegram (admin / owner / doctor bots).

Staff always get the shared ARIX bot (or the tenant bot, if it is the same
token) — a separate token per role would mean three QR codes per clinic for no
benefit.  Roles are enforced by the ``staff`` table, not by which bot wrote.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.channels.base import Button, OutboundMessage
from arix.channels.registry import get_channel
from arix.channels.telegram import TelegramAdapter
from arix.config import get_settings
from arix.core.context import TenantContext
from arix.core.crypto import decrypt_json
from arix.db.models_core import Channel, Staff
from arix.logging_setup import get_logger

log = get_logger("arix.notify")


async def staff_bot(session: AsyncSession, ctx: TenantContext):
    """Resolve the transport used to talk to this tenant's staff.

    Returns a :class:`DryRunAdapter` when deliveries are disabled, so tests,
    evals and offline demos exercise the same code path.
    """
    s = get_settings()
    if s.channels_dry_run:
        from arix.channels.dryrun import DryRunAdapter

        return DryRunAdapter(f"staff:{ctx.id}")
    token = s.telegram_shared_bot_token
    channel = (
        await session.execute(
            select(Channel).where(
                Channel.tenant_id == ctx.id,
                Channel.type.in_(["tg", "tg_business"]),
            )
        )
    ).scalars().first()
    if channel is not None:
        creds = decrypt_json(channel.credentials_enc)
        token = creds.get("staff_bot_token") or creds.get("bot_token") or token
    if not token:
        return None
    return TelegramAdapter(token)


async def notify_staff(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    roles: list[str],
    text: str,
    buttons: list[tuple[str, str]] | None = None,
    staff_ids: list[uuid.UUID] | None = None,
) -> int:
    """Send ``text`` to every active staff member in ``roles``. Returns count."""
    adapter = await staff_bot(session, ctx)
    if adapter is None:
        log.warning("no_staff_bot", tenant=str(ctx.id))
        return 0

    stmt = select(Staff).where(
        Staff.tenant_id == ctx.id, Staff.active.is_(True), Staff.tg_user_id.isnot(None)
    )
    if staff_ids:
        stmt = stmt.where(Staff.id.in_(staff_ids))
    else:
        stmt = stmt.where(Staff.role.in_(roles))
    recipients = list((await session.execute(stmt)).scalars())

    sent = 0
    for member in recipients:
        result = await adapter.send(
            OutboundMessage(
                peer_id=str(member.tg_user_id),
                text=text,
                buttons=[Button(label, data) for label, data in (buttons or [])],
            )
        )
        if result.ok:
            sent += 1
        else:
            log.warning(
                "staff_notify_failed", staff=str(member.id), error=result.error
            )
    return sent


async def notify_one(
    session: AsyncSession,
    ctx: TenantContext,
    tg_user_id: int,
    text: str,
    buttons: list[tuple[str, str]] | None = None,
) -> bool:
    adapter = await staff_bot(session, ctx)
    if adapter is None:
        return False
    result = await adapter.send(
        OutboundMessage(
            peer_id=str(tg_user_id),
            text=text,
            buttons=[Button(label, data) for label, data in (buttons or [])],
        )
    )
    return result.ok


async def has_channel(session: AsyncSession, ctx: TenantContext, channel_type: str) -> bool:
    return await get_channel(session, ctx.id, channel_type) is not None


__all__ = ["has_channel", "notify_one", "notify_staff", "staff_bot"]
