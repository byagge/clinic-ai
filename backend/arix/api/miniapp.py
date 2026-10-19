"""Mini App API (Telegram WebApp).

Authentication is the Telegram ``initData`` signature: we verify it against
the bot token, so a patient cannot read someone else's visits by changing an
id in the URL.  Every endpoint derives the contact from the verified Telegram
user, never from the request body.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import date
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from arix.core.context import TenantContext, load_context_by_slug
from arix.core.crypto import decrypt_json
from arix.core.timeutil import humanize_local, now_utc, to_tenant
from arix.db.models_core import Channel, Contact, ContactIdentity, Conversation
from arix.db.session import tenant_session
from arix.verticals.clinic import booking as bk
from arix.verticals.clinic.scheduling import schedule_visit_jobs

router = APIRouter(prefix="/miniapp/{slug}", tags=["miniapp"])

INIT_DATA_TTL_SECONDS = 24 * 3600


class MiniAppBooking(BaseModel):
    slot_key: str
    name: str | None = None
    phone: str | None = None
    consent_pd: bool = False


class MiniAppPayment(BaseModel):
    visit_id: uuid.UUID
    amount: float


def verify_init_data(init_data: str, bot_token: str) -> dict:
    """Verify Telegram WebApp ``initData`` (HMAC over the sorted fields)."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    provided = pairs.pop("hash", None)
    if not provided:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "initData without hash")
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad initData signature")
    auth_date = int(pairs.get("auth_date") or 0)
    if auth_date and (now_utc().timestamp() - auth_date) > INIT_DATA_TTL_SECONDS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "initData expired")
    user = json.loads(pairs.get("user") or "{}")
    if not user.get("id"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "initData without user")
    return user


class MiniAppScope:
    def __init__(self, session, ctx: TenantContext, contact: Contact, conversation: Conversation):
        self.session = session
        self.ctx = ctx
        self.contact = contact
        self.conversation = conversation


async def miniapp_scope(
    slug: str,
    x_telegram_init_data: Annotated[str | None, Header()] = None,
):
    if not x_telegram_init_data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-Telegram-Init-Data required")
    async with tenant_session(None, bypass_rls=True) as probe:
        ctx = await load_context_by_slug(probe, slug)
    if ctx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown tenant")

    async with tenant_session(ctx.id) as session:
        channel = (
            await session.execute(
                select(Channel).where(
                    Channel.tenant_id == ctx.id, Channel.type.in_(["tg", "tg_business"])
                )
            )
        ).scalars().first()
        creds = decrypt_json(channel.credentials_enc) if channel else {}
        bot_token = creds.get("bot_token")
        if not bot_token:
            from arix.config import get_settings

            bot_token = get_settings().telegram_shared_bot_token
        if not bot_token:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "telegram is not connected")

        user = verify_init_data(x_telegram_init_data, bot_token)
        peer_id = str(user["id"])

        identity = (
            await session.execute(
                select(ContactIdentity).where(
                    ContactIdentity.tenant_id == ctx.id,
                    ContactIdentity.channel_type == "tg",
                    ContactIdentity.external_id == peer_id,
                )
            )
        ).scalar_one_or_none()
        contact = None
        if identity is not None:
            contact = (
                await session.execute(select(Contact).where(Contact.id == identity.contact_id))
            ).scalar_one_or_none()
        if contact is None:
            contact = Contact(
                tenant_id=ctx.id,
                name=" ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or None,
                lang=ctx.lang_for(user.get("language_code")),
                source="miniapp",
            )
            session.add(contact)
            await session.flush()
            session.add(
                ContactIdentity(
                    tenant_id=ctx.id, contact_id=contact.id,
                    channel_type="tg", external_id=peer_id,
                )
            )
        conversation = (
            await session.execute(
                select(Conversation).where(
                    Conversation.tenant_id == ctx.id,
                    Conversation.channel_type == "tg",
                    Conversation.peer_id == peer_id,
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            conversation = Conversation(
                tenant_id=ctx.id, contact_id=contact.id, channel_id=channel.id if channel else None,
                channel_type="tg", peer_id=peer_id, state="active",
            )
            session.add(conversation)
        await session.flush()
        yield MiniAppScope(session, ctx, contact, conversation)


Scope = Annotated[MiniAppScope, Depends(miniapp_scope)]


@router.get("/profile")
async def profile(scope: Scope) -> dict:
    return {
        "tenant": {
            "name": scope.ctx.name,
            "city": scope.ctx.tenant.city,
            "address": scope.ctx.settings.address,
            "map_url": scope.ctx.settings.map_url,
            "phone": scope.ctx.settings.phone,
            "currency": scope.ctx.currency,
            "languages": scope.ctx.languages,
            "payments_enabled": scope.ctx.settings.features.miniapp_payments,
        },
        "contact": {
            "name": scope.contact.name,
            "phone": scope.contact.phone,
            "consent_pd": scope.contact.consent_pd_at is not None,
            "lang": scope.contact.lang,
        },
    }


@router.get("/services")
async def services(scope: Scope, category: str | None = None) -> list[dict]:
    rows = await bk.list_services(scope.session, scope.ctx.id, category=category, limit=100)
    return [
        {
            "service_id": str(row.id),
            "name": row.label(scope.contact.lang),
            "category": row.category,
            "price": bk.service_price_text(row),
            "price_from": float(row.price_from) if row.price_from is not None else None,
            "duration_min": row.duration_min,
            "online_bookable": row.online_bookable,
        }
        for row in rows
    ]


@router.get("/doctors")
async def doctors(scope: Scope, service_id: uuid.UUID | None = None) -> list[dict]:
    rows = await bk.list_doctors(scope.session, scope.ctx.id, service_id=service_id)
    return [
        {
            "doctor_id": str(row.id),
            "name": row.name,
            "specialty": row.specialty,
            "langs": list(row.langs or []),
            "bio": row.bio_short,
            "photo_url": row.photo_url,
        }
        for row in rows
    ]


@router.get("/slots")
async def slots(
    scope: Scope,
    service_id: uuid.UUID,
    doctor_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    count: int = Query(default=12, le=50),
) -> list[dict]:
    offers = await bk.find_slots(
        scope.session, scope.ctx,
        service_id=service_id, doctor_id=doctor_id,
        date_from=date_from, date_to=date_to, count=count,
    )
    tz = scope.ctx.tz_name
    return [
        {
            "slot_key": o.key,
            "doctor": o.doctor_name,
            "doctor_id": str(o.doctor_id),
            "date": to_tenant(o.start, tz).strftime("%Y-%m-%d"),
            "time": to_tenant(o.start, tz).strftime("%H:%M"),
            "when": humanize_local(o.start, tz, scope.contact.lang),
        }
        for o in offers
    ]


@router.post("/book", status_code=201)
async def book(body: MiniAppBooking, scope: Scope) -> dict:
    from arix.core.pii import normalize_phone

    try:
        doctor_id, service_id, start = bk.SlotOffer.parse_key(body.slot_key)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad slot_key") from exc

    if body.name:
        scope.contact.name = body.name[:160]
    if body.phone:
        phone = normalize_phone(body.phone, scope.ctx.tenant.country)
        if phone:
            scope.contact.phone = phone
    if body.consent_pd and scope.contact.consent_pd_at is None:
        scope.contact.consent_pd_at = now_utc()

    missing = [
        field
        for field, ok in (
            ("name", bool(scope.contact.name)),
            ("phone", bool(scope.contact.phone)),
            ("consent_pd", scope.contact.consent_pd_at is not None),
        )
        if not ok
    ]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, {"error": "missing_fields", "missing": missing}
        )

    service = await bk.get_service(scope.session, scope.ctx.id, service_id)
    if service is None or not service.online_bookable:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "service is not bookable online")

    try:
        visit = await bk.hold_slot(
            scope.session, scope.ctx,
            contact_id=scope.contact.id, doctor_id=doctor_id, service_id=service_id,
            start=start, source="miniapp", attribution="miniapp",
            idempotency_key=f"miniapp:{scope.contact.id}:{body.slot_key}",
        )
    except bk.SlotTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slot is already taken") from exc

    await bk.confirm_visit(scope.session, scope.ctx, visit.id)
    await schedule_visit_jobs(scope.session, scope.ctx, visit.id)
    refs = await bk.visit_with_refs(scope.session, scope.ctx.id, visit.id)
    assert refs is not None
    v, doctor, svc, _contact = refs
    return {
        "visit_id": str(v.id),
        "when": humanize_local(v.starts_at, scope.ctx.tz_name, scope.contact.lang),
        "doctor": doctor.name,
        "service": svc.label(scope.contact.lang),
        "address": scope.ctx.settings.address,
        "map_url": scope.ctx.settings.map_url,
    }


@router.get("/my/visits")
async def my_visits(scope: Scope) -> list[dict]:
    visits = await bk.upcoming_visits(
        scope.session, scope.ctx.id, scope.contact.id, include_past_hours=24 * 90
    )
    out = []
    for visit in visits:
        refs = await bk.visit_with_refs(scope.session, scope.ctx.id, visit.id)
        if refs is None:
            continue
        _, doctor, service, _ = refs
        out.append(
            {
                "visit_id": str(visit.id),
                "starts_at": visit.starts_at,
                "when": humanize_local(visit.starts_at, scope.ctx.tz_name, scope.contact.lang),
                "doctor": doctor.name,
                "service": service.label(scope.contact.lang),
                "status": visit.status,
                "price_from": float(visit.price_quoted) if visit.price_quoted else None,
            }
        )
    return out


@router.post("/my/visits/{visit_id}/cancel")
async def cancel(visit_id: uuid.UUID, scope: Scope) -> dict:
    from arix.verticals.clinic.scheduling import cancel_visit_jobs

    visit = (
        await scope.session.execute(
            select(bk.Visit).where(
                bk.Visit.tenant_id == scope.ctx.id,
                bk.Visit.id == visit_id,
                bk.Visit.contact_id == scope.contact.id,
            )
        )
    ).scalar_one_or_none()
    if visit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown visit")
    await bk.cancel_visit(scope.session, scope.ctx, visit_id, "miniapp")
    await cancel_visit_jobs(scope.session, scope.ctx, visit_id)
    return {"ok": True}


@router.post("/my/visits/{visit_id}/reschedule")
async def reschedule(visit_id: uuid.UUID, slot_key: str, scope: Scope) -> dict:
    from arix.verticals.clinic.scheduling import cancel_visit_jobs

    try:
        doctor_id, _service_id, start = bk.SlotOffer.parse_key(slot_key)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad slot_key") from exc
    owned = (
        await scope.session.execute(
            select(bk.Visit.id).where(
                bk.Visit.tenant_id == scope.ctx.id,
                bk.Visit.id == visit_id,
                bk.Visit.contact_id == scope.contact.id,
            )
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown visit")
    try:
        moved = await bk.reschedule(
            scope.session, scope.ctx, visit_id, start, new_doctor_id=doctor_id
        )
    except bk.SlotTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slot is already taken") from exc
    await cancel_visit_jobs(scope.session, scope.ctx, visit_id)
    await schedule_visit_jobs(scope.session, scope.ctx, moved.id)
    return {
        "visit_id": str(moved.id),
        "when": humanize_local(moved.starts_at, scope.ctx.tz_name, scope.contact.lang),
    }


@router.post("/pay")
async def pay(body: MiniAppPayment, scope: Scope) -> dict:
    from arix.payments import create_payment

    if not scope.ctx.settings.features.miniapp_payments:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "payments are disabled for this tenant")
    owned = (
        await scope.session.execute(
            select(bk.Visit.id).where(
                bk.Visit.tenant_id == scope.ctx.id,
                bk.Visit.id == body.visit_id,
                bk.Visit.contact_id == scope.contact.id,
            )
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown visit")
    payment, url = await create_payment(
        scope.session, scope.ctx,
        contact_id=scope.contact.id, amount=body.amount,
        entity_ref=str(body.visit_id), attributed_to="miniapp",
    )
    return {"payment_id": str(payment.id), "url": url, "provider": payment.provider}


__all__ = ["router", "verify_init_data"]
