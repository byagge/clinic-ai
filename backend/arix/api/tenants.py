"""ARIX superadmin surface: tenants, config, channels, staff, demo."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from arix.config import get_settings
from arix.core.context import load_context
from arix.core.crypto import decrypt_json
from arix.core.events import audit
from arix.db.models_core import Channel, Staff, Tenant
from arix.seed.provision import (
    create_tenant,
    ensure_channel,
    ensure_staff,
    publish_config,
    seed_demo_data,
)
from arix.seed.template_packs import load_all, pick_by_rubrics

from .deps import (
    Principal,
    TenantSession,
    client_ip,
    db_admin,
    require_superadmin,
    tenant_scope,
)
from .schemas import (
    ChannelConnectTelegram,
    ChannelConnectWhatsApp,
    ConfigPatch,
    DemoRequest,
    DemoResponse,
    StaffCreate,
    TenantCreate,
    TenantOut,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/templates")
async def list_templates(_: Annotated[Principal, Depends(require_superadmin)]) -> list[dict]:
    return [
        {
            "code": t.code,
            "vertical": t.vertical,
            "title": t.title,
            "rubrics": t.rubrics,
            "services": len(t.services),
            "doctors": len(t.doctors),
            "courses": len(t.courses),
            "kb": len(t.kb),
        }
        for t in load_all().values()
    ]


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(
    _: Annotated[Principal, Depends(require_superadmin)],
    session=Depends(db_admin),
    status_filter: str | None = None,
) -> list[Tenant]:
    stmt = select(Tenant).order_by(Tenant.created_at.desc())
    if status_filter:
        stmt = stmt.where(Tenant.status == status_filter)
    return list((await session.execute(stmt)).scalars())


@router.post("/tenants", response_model=TenantOut, status_code=201)
async def create_tenant_endpoint(
    body: TenantCreate,
    request: Request,
    _: Annotated[Principal, Depends(require_superadmin)],
    session=Depends(db_admin),
) -> Tenant:
    s = get_settings()
    if body.vertical != s.vertical:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"this deployment serves the {s.vertical!r} vertical only",
        )
    result = await create_tenant(
        session,
        slug=body.slug,
        name=body.name,
        vertical=body.vertical,
        country=body.country,
        city=body.city,
        timezone=body.timezone,
        languages=body.languages,
        template_code=body.template,
        status=body.status,
        plan=body.plan,
    )
    await audit(
        session, result.tenant.id, actor="superadmin", action="tenant_created",
        ip=client_ip(request), data={"slug": body.slug},
    )
    return result.tenant


@router.get("/tenants/{tenant_id}", response_model=TenantOut)
async def get_tenant(scope: Annotated[TenantSession, Depends(tenant_scope)]) -> Tenant:
    return scope.ctx.tenant


@router.patch("/tenants/{tenant_id}/status", response_model=TenantOut)
async def set_tenant_status(
    tenant_id: uuid.UUID,
    new_status: str,
    _: Annotated[Principal, Depends(require_superadmin)],
    session=Depends(db_admin),
) -> Tenant:
    if new_status not in ("demo", "pilot", "active", "paused", "archived"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad status")
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown tenant")
    tenant.status = new_status
    await session.flush()
    return tenant


@router.get("/tenants/{tenant_id}/config")
async def get_config(scope: Annotated[TenantSession, Depends(tenant_scope)]) -> dict:
    return {
        "version": scope.ctx.config_version,
        "data": scope.ctx.settings.model_dump(mode="json"),
    }


@router.put("/tenants/{tenant_id}/config")
async def put_config(
    tenant_id: uuid.UUID,
    body: ConfigPatch,
    scope: Annotated[TenantSession, Depends(tenant_scope)],
) -> dict:
    row = await publish_config(scope.session, tenant_id, body.data, note=body.note)
    return {"version": row.version, "published_at": row.published_at}


@router.post("/tenants/{tenant_id}/channels/telegram")
async def connect_telegram(
    tenant_id: uuid.UUID,
    body: ChannelConnectTelegram,
    scope: Annotated[TenantSession, Depends(tenant_scope)],
) -> dict:
    """Attach a Telegram bot (own token or the shared ARIX bot).

    With ``business=True`` the clinic links its own Telegram Business account:
    the bot token stays ours, patients keep writing to the clinic's account,
    and replies carry the ``business_connection_id`` we learn from the
    ``business_connection`` update.
    """
    s = get_settings()
    token = body.bot_token or s.telegram_shared_bot_token
    if not token:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "no bot token provided and no shared bot configured"
        )
    credentials = {"bot_token": token}
    if body.staff_bot_token:
        credentials["staff_bot_token"] = body.staff_bot_token

    channel = await ensure_channel(
        scope.session, tenant_id,
        "tg_business" if body.business else "tg",
        credentials=credentials,
        settings={"shared_bot": body.bot_token is None},
        status="connecting",
    )

    webhook_url = f"{s.public_base_url.rstrip('/')}/webhooks/tg/{scope.ctx.tenant.slug}"
    result: dict = {"channel_id": str(channel.id), "webhook": webhook_url}
    if body.set_webhook:
        from arix.channels.telegram import TelegramAdapter

        adapter = TelegramAdapter(token)
        health = await adapter.health()
        hook = await adapter.set_webhook(webhook_url, s.telegram_webhook_secret)
        channel.status = "connected" if hook.get("ok") else "error"
        channel.status_detail = None if hook.get("ok") else str(hook.get("description"))[:500]
        channel.display_name = health.get("bot")
        await scope.session.flush()
        result |= {"bot": health.get("bot"), "webhook_set": bool(hook.get("ok"))}
        if health.get("bot"):
            result["deeplink"] = f"https://t.me/{health['bot']}?start=t_{scope.ctx.tenant.slug}"
    return result


@router.post("/tenants/{tenant_id}/channels/whatsapp")
async def connect_whatsapp(
    tenant_id: uuid.UUID,
    body: ChannelConnectWhatsApp,
    scope: Annotated[TenantSession, Depends(tenant_scope)],
) -> dict:
    """Start a WhatsApp channel.

    ``mode=baileys`` (default) starts a sidecar session; the clinic then scans
    the QR from ``GET .../channels/whatsapp/qr`` once and the number is linked
    with no Meta fees.  ``mode=cloud_api`` stores official credentials instead.
    """
    s = get_settings()
    credentials: dict = {}
    settings_patch = {"mode": body.mode, "session_id": scope.ctx.tenant.slug}
    if body.mode == "cloud_api":
        if not (body.phone_number_id and body.access_token):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "phone_number_id and access_token are required"
            )
        credentials = {
            "phone_number_id": body.phone_number_id,
            "access_token": body.access_token,
            "app_secret": body.app_secret,
            "verify_token": body.verify_token,
        }
    channel = await ensure_channel(
        scope.session, tenant_id, "wa",
        credentials=credentials or None, settings=settings_patch, status="connecting",
    )
    out: dict = {"channel_id": str(channel.id), "mode": body.mode}
    if body.mode == "baileys":
        from arix.channels.whatsapp import BaileysAdapter

        adapter = BaileysAdapter(scope.ctx.tenant.slug)
        started = await adapter.start()
        out |= {
            "started": started,
            "webhook": f"{s.public_base_url.rstrip('/')}/webhooks/wa/baileys/{scope.ctx.tenant.slug}",
            "next": "Откройте .../channels/whatsapp/qr и отсканируйте QR в WhatsApp → "
            "Связанные устройства",
        }
    else:
        out["webhook"] = (
            f"{s.public_base_url.rstrip('/')}/webhooks/wa/cloud/{scope.ctx.tenant.slug}"
        )
    return out


@router.get("/tenants/{tenant_id}/channels/whatsapp/qr")
async def whatsapp_qr(
    tenant_id: uuid.UUID, scope: Annotated[TenantSession, Depends(tenant_scope)]
) -> dict:
    from arix.channels.whatsapp import BaileysAdapter

    adapter = BaileysAdapter(scope.ctx.tenant.slug)
    return await adapter.qr()


@router.get("/tenants/{tenant_id}/channels")
async def list_channels(scope: Annotated[TenantSession, Depends(tenant_scope)]) -> list[dict]:
    rows = (
        await scope.session.execute(
            select(Channel).where(Channel.tenant_id == scope.ctx.id)
        )
    ).scalars()
    out = []
    for channel in rows:
        creds = decrypt_json(channel.credentials_enc)
        out.append(
            {
                "id": str(channel.id),
                "type": channel.type,
                "status": channel.status,
                "status_detail": channel.status_detail,
                "display_name": channel.display_name,
                "settings": {k: v for k, v in (channel.settings or {}).items() if k != "qr"},
                "has_credentials": bool(creds),
                "last_seen_at": channel.last_seen_at,
            }
        )
    return out


@router.post("/tenants/{tenant_id}/staff", status_code=201)
async def add_staff(
    tenant_id: uuid.UUID,
    body: StaffCreate,
    scope: Annotated[TenantSession, Depends(tenant_scope)],
) -> dict:
    if body.role not in ("owner", "admin", "doctor", "teacher"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad role")
    member = await ensure_staff(
        scope.session, tenant_id,
        role=body.role, name=body.name, tg_user_id=body.tg_user_id,
        email=body.email, password=body.password, entity_id=body.entity_id,
    )
    return {"id": str(member.id), "role": member.role, "name": member.name}


@router.get("/tenants/{tenant_id}/staff")
async def list_staff(scope: Annotated[TenantSession, Depends(tenant_scope)]) -> list[dict]:
    rows = (
        await scope.session.execute(select(Staff).where(Staff.tenant_id == scope.ctx.id))
    ).scalars()
    return [
        {
            "id": str(row.id),
            "role": row.role,
            "name": row.name,
            "tg_user_id": row.tg_user_id,
            "email": row.email,
            "active": row.active,
        }
        for row in rows
    ]


@router.post("/demo", response_model=DemoResponse, status_code=201)
async def generate_demo(
    body: DemoRequest,
    _: Annotated[Principal, Depends(require_superadmin)],
    session=Depends(db_admin),
) -> DemoResponse:
    """Demo generator for outreach (ТЗ §12): a 2GIS card in, a live bot out."""
    s = get_settings()
    if not s.demo_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "demo generation is disabled")

    template = pick_by_rubrics(body.rubrics, vertical=s.vertical, default="clinic_dental")
    slug = _slugify(body.name, body.city)
    timezone = body.timezone or ("Asia/Tashkent" if body.country.upper() == "UZ" else "Asia/Almaty")
    languages = body.languages or (["uz", "ru"] if body.country.upper() == "UZ" else ["ru", "kz"])

    result = await create_tenant(
        session,
        slug=slug,
        name=body.name,
        vertical=s.vertical,
        country=body.country,
        city=body.city,
        timezone=timezone,
        languages=languages,
        template_code=template.code,
        status="demo",
        plan="demo",
        source_ref={"source": "2gis", "link": body.link, "rubrics": body.rubrics},
    )
    tenant = result.tenant

    overrides: dict = {
        "display_name": body.name,
        "address": body.address,
        "phone": body.phone,
        "currency": "UZS" if body.country.upper() == "UZ" else "KZT",
        "agent": {
            "extra_rules": [
                "Цены в этом демо примерные: если клиент уточняет точную цену, скажи, что "
                "уточнишь у администратора."
            ]
        },
    }
    if body.working_hours:
        overrides["working_hours"] = {"mon-sun": body.working_hours}
    await publish_config(session, tenant.id, overrides, note="demo_card")

    seeded = dict(result.counts)
    ctx = await load_context(session, tenant.id)
    seeded |= await seed_demo_data(session, ctx)

    deeplink = None
    if s.telegram_shared_bot_token:
        await ensure_channel(
            session, tenant.id, "tg",
            credentials={"bot_token": s.telegram_shared_bot_token},
            settings={"shared_bot": True}, status="connected",
        )
        from arix.channels.telegram import TelegramAdapter

        health = await TelegramAdapter(s.telegram_shared_bot_token).health()
        if health.get("bot"):
            deeplink = f"https://t.me/{health['bot']}?start=t_{slug}"

    return DemoResponse(
        tenant_id=tenant.id,
        slug=slug,
        template=template.code,
        telegram_link=deeplink,
        expires_at=tenant.demo_expires_at,
        seeded=seeded,
    )


def _slugify(name: str, city: str | None) -> str:
    import re
    from unicodedata import normalize

    translit = str.maketrans(
        {
            "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
            "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
            "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
            "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
            "я": "ya", "қ": "q", "ғ": "g", "ң": "ng", "ө": "o", "ұ": "u", "ү": "u", "һ": "h",
            "і": "i",
        }
    )
    base = f"{name} {city or ''}".lower().translate(translit)
    base = normalize("NFKD", base).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:48] or "demo"
    return f"{slug}-{uuid.uuid4().hex[:5]}"


__all__ = ["router"]
