"""Payment links and webhooks (Kaspi via intermediary, Click, Payme).

ARIX never holds money: a link is created at the provider, the patient pays
the clinic directly, and we only record the fact and the attribution so the
revenue-share report (ТЗ §10) can be computed.

Each provider gets a thin class with ``create_link`` and ``verify_webhook``.
Without credentials the module falls back to an internal confirmation page,
which is what demos use.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.config import get_settings
from arix.core.context import TenantContext
from arix.core.crypto import decrypt_json
from arix.core.events import PAYMENT_PAID, log_event
from arix.core.timeutil import now_utc
from arix.db.models_core import Channel, Payment

PROVIDERS = ("kaspi", "click", "payme", "manual")


@dataclass
class ProviderConfig:
    provider: str
    merchant_id: str | None = None
    secret: str | None = None
    base_url: str | None = None


def default_provider(country: str) -> str:
    return {"KZ": "kaspi", "UZ": "click"}.get(country.upper(), "manual")


async def provider_config(
    session: AsyncSession, ctx: TenantContext
) -> ProviderConfig:
    row = (
        await session.execute(
            select(Channel).where(Channel.tenant_id == ctx.id, Channel.type == "miniapp")
        )
    ).scalar_one_or_none()
    creds = decrypt_json(row.credentials_enc) if row else {}
    provider = creds.get("payment_provider") or default_provider(ctx.tenant.country)
    return ProviderConfig(
        provider=provider,
        merchant_id=creds.get("merchant_id"),
        secret=creds.get("payment_secret"),
        base_url=creds.get("payment_base_url"),
    )


async def create_payment(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    contact_id: uuid.UUID,
    amount: float | Decimal,
    entity_ref: str | None = None,
    attributed_to: str = "miniapp",
    currency: str | None = None,
) -> tuple[Payment, str]:
    config = await provider_config(session, ctx)
    payment = Payment(
        tenant_id=ctx.id,
        contact_id=contact_id,
        provider=config.provider,
        amount=Decimal(str(amount)),
        currency=currency or ctx.currency,
        status="created",
        attributed_to=attributed_to,
        entity_ref=entity_ref,
        external_id=str(uuid.uuid4()),
    )
    session.add(payment)
    await session.flush()

    s = get_settings()
    if config.provider == "click" and config.merchant_id:
        url = (
            f"https://my.click.uz/services/pay?service_id={config.merchant_id}"
            f"&merchant_trans_id={payment.id}&amount={payment.amount}"
        )
    elif config.provider == "payme" and config.merchant_id:
        import base64

        raw = f"m={config.merchant_id};ac.order_id={payment.id};a={int(payment.amount) * 100}"
        url = f"https://checkout.paycom.uz/{base64.b64encode(raw.encode()).decode()}"
    elif config.provider == "kaspi" and config.base_url:
        url = f"{config.base_url.rstrip('/')}?order={payment.id}&amount={payment.amount}"
    else:
        # Demo / manual: internal page the admin confirms.
        url = f"{s.public_base_url}/pay/{payment.id}"
    payment.meta = {**(payment.meta or {}), "url": url}
    await session.flush()
    return payment, url


async def mark_paid(
    session: AsyncSession,
    ctx: TenantContext,
    payment_id: uuid.UUID,
    *,
    provider_ref: str | None = None,
) -> Payment | None:
    payment = (
        await session.execute(
            select(Payment).where(Payment.tenant_id == ctx.id, Payment.id == payment_id)
        )
    ).scalar_one_or_none()
    if payment is None or payment.status == "paid":
        return payment
    payment.status = "paid"
    payment.paid_at = now_utc()
    if provider_ref:
        payment.meta = {**(payment.meta or {}), "provider_ref": provider_ref}
    await session.flush()
    await log_event(
        session,
        ctx.id,
        PAYMENT_PAID,
        contact_id=payment.contact_id,
        entity_ref=payment.entity_ref or str(payment.id),
        amount=payment.amount,
        currency=payment.currency,
        source=payment.attributed_to,
    )
    return payment


def verify_click_signature(payload: dict, secret: str) -> bool:
    """Click sends ``sign_string`` = md5 of a fixed field concatenation."""
    expected = hashlib.md5(
        "".join(
            str(payload.get(field, ""))
            for field in (
                "click_trans_id",
                "service_id",
            )
        ).encode()
        + secret.encode()
        + "".join(
            str(payload.get(field, ""))
            for field in ("merchant_trans_id", "amount", "action", "sign_time")
        ).encode()
    ).hexdigest()
    return hmac.compare_digest(expected, str(payload.get("sign_string", "")))


__all__ = [
    "PROVIDERS",
    "ProviderConfig",
    "create_payment",
    "default_provider",
    "mark_paid",
    "provider_config",
    "verify_click_signature",
]
