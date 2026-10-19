"""Tenant provisioning: create a tenant and seed it from a template.

This is what makes "настройка новой клиники ≤ 4 часов" true — and the demo
generator reach 60 seconds.  Idempotent per slug: re-running updates config and
tops up missing rows instead of duplicating them.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.config import get_settings
from arix.core.context import TenantContext
from arix.core.events import audit
from arix.core.timeutil import now_utc, parse_hhmm
from arix.db.models_core import Channel, KBItem, Sequence, Staff, Tenant, TenantConfig
from arix.logging_setup import get_logger
from arix.verticals.clinic.models import (
    Doctor,
    DoctorService,
    Schedule,
    Service,
)

from .template_packs import Template, get_template

log = get_logger("arix.provision")


@dataclass
class ProvisionResult:
    tenant: Tenant
    created: bool
    counts: dict[str, int]


async def create_tenant(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    vertical: str = "clinic",
    country: str = "KZ",
    city: str | None = None,
    timezone: str = "Asia/Almaty",
    languages: list[str] | None = None,
    template_code: str | None = "clinic_dental",
    status: str = "pilot",
    plan: str = "pilot",
    demo_ttl_days: int | None = None,
    source_ref: dict | None = None,
) -> ProvisionResult:
    existing = (
        await session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    created = existing is None
    s = get_settings()

    if existing is None:
        tenant = Tenant(
            slug=slug,
            name=name,
            vertical=vertical,
            country=country.upper(),
            city=city,
            timezone=timezone,
            languages=languages or ["ru"],
            plan=plan,
            status=status,
            data_region=s.data_region,
            template=template_code,
            source_ref=source_ref,
            demo_expires_at=(
                now_utc() + timedelta(days=demo_ttl_days or s.demo_ttl_days)
                if status == "demo"
                else None
            ),
        )
        session.add(tenant)
        await session.flush()
    else:
        tenant = existing
        tenant.name = name
        tenant.city = city or tenant.city
        tenant.timezone = timezone or tenant.timezone
        if languages:
            tenant.languages = languages
        tenant.template = template_code or tenant.template
        await session.flush()

    template = get_template(template_code)
    counts: dict[str, int] = {}
    if template is not None:
        counts = await apply_template(session, tenant, template, languages=languages)

    await audit(
        session, tenant.id, actor="system", action="tenant_provisioned",
        entity="tenant", entity_id=str(tenant.id),
        data={"template": template_code, "created": created, "counts": counts},
    )
    log.info("tenant_provisioned", slug=slug, created=created, counts=counts)
    return ProvisionResult(tenant=tenant, created=created, counts=counts)


async def apply_template(
    session: AsyncSession,
    tenant: Tenant,
    template: Template,
    *,
    languages: list[str] | None = None,
    overrides: dict | None = None,
) -> dict[str, int]:
    counts = {"services": 0, "doctors": 0, "schedules": 0, "kb": 0, "sequences": 0}

    config = dict(template.config)
    if overrides:
        config = _deep_merge(config, overrides)
    await publish_config(session, tenant.id, config, note=f"template:{template.code}")

    existing_services = {
        row.code: row
        for row in (
            await session.execute(select(Service).where(Service.tenant_id == tenant.id))
        ).scalars()
    }
    service_by_code: dict[str, Service] = dict(existing_services)
    for item in template.services:
        if item["code"] in existing_services:
            continue
        service = Service(
            tenant_id=tenant.id,
            code=item["code"],
            name=item.get("name") or {"ru": item["code"]},
            category=item.get("category"),
            price_from=item.get("price_from"),
            price_to=item.get("price_to"),
            currency=config.get("currency", "KZT"),
            duration_min=int(item.get("duration_min", 30)),
            buffer_min=int(item.get("buffer_min", 0)),
            recall_days=item.get("recall_days"),
            needs_consultation_first=bool(item.get("needs_consultation_first")),
            online_bookable=bool(item.get("online_bookable", True)),
            complaint_keywords=item.get("complaint_keywords") or [],
            sort=int(item.get("sort", 100)),
        )
        session.add(service)
        service_by_code[item["code"]] = service
        counts["services"] += 1
    await session.flush()

    existing_doctors = {
        row.name: row
        for row in (
            await session.execute(select(Doctor).where(Doctor.tenant_id == tenant.id))
        ).scalars()
    }
    for index, item in enumerate(template.doctors):
        if item["name"] in existing_doctors:
            continue
        doctor = Doctor(
            tenant_id=tenant.id,
            name=item["name"],
            specialty=item.get("specialty"),
            langs=item.get("langs") or languages or ["ru"],
            bio_short=item.get("bio_short"),
            sort=index,
        )
        session.add(doctor)
        await session.flush()
        counts["doctors"] += 1

        for code in item.get("services", []):
            service = service_by_code.get(code)
            if service is not None:
                session.add(
                    DoctorService(
                        tenant_id=tenant.id, doctor_id=doctor.id, service_id=service.id
                    )
                )

        schedule = item.get("schedule") or {}
        start = parse_hhmm(schedule.get("start", "09:00"))
        end = parse_hhmm(schedule.get("end", "18:00"))
        break_pair = schedule.get("break") or []
        break_start = parse_hhmm(break_pair[0]) if len(break_pair) == 2 else None
        break_end = parse_hhmm(break_pair[1]) if len(break_pair) == 2 else None
        for weekday in schedule.get("weekdays", [0, 1, 2, 3, 4]):
            session.add(
                Schedule(
                    tenant_id=tenant.id,
                    doctor_id=doctor.id,
                    weekday=int(weekday),
                    start=start,
                    end=end,
                    break_start=break_start,
                    break_end=break_end,
                )
            )
            counts["schedules"] += 1
    await session.flush()

    existing_kb = {
        row.question
        for row in (
            await session.execute(select(KBItem.question).where(KBItem.tenant_id == tenant.id))
        ).scalars()
    }
    for item in template.kb:
        if item["question"] in existing_kb:
            continue
        session.add(
            KBItem(
                tenant_id=tenant.id,
                type=item.get("type", "faq"),
                question=item["question"],
                answer=item["answer"],
                lang=item.get("lang", "ru"),
                tags=item.get("tags") or [],
                verbatim=bool(item.get("verbatim")),
            )
        )
        counts["kb"] += 1

    existing_sequences = {
        row.code
        for row in (
            await session.execute(select(Sequence.code).where(Sequence.tenant_id == tenant.id))
        ).scalars()
    }
    for code, body in (template.sequences or {}).items():
        if code in existing_sequences:
            continue
        session.add(Sequence(tenant_id=tenant.id, code=code, steps=body))
        counts["sequences"] += 1

    await session.flush()
    return counts


async def publish_config(
    session: AsyncSession, tenant_id: uuid.UUID, data: dict, *, note: str | None = None
) -> TenantConfig:
    """Store a new config version and publish it.

    Versions are append-only: rolling back is choosing an older version, never
    editing history.
    """
    from arix.core.tenant_config import TenantSettings

    TenantSettings.from_data(data)  # validate before publishing

    current = (
        await session.execute(
            select(TenantConfig)
            .where(TenantConfig.tenant_id == tenant_id)
            .order_by(TenantConfig.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    merged = _deep_merge(current.data if current else {}, data)
    row = TenantConfig(
        tenant_id=tenant_id,
        version=(current.version + 1) if current else 1,
        data=merged,
        published_at=now_utc(),
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def ensure_channel(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    channel_type: str,
    *,
    credentials: dict | None = None,
    settings: dict | None = None,
    status: str = "pending",
    external_id: str | None = None,
) -> Channel:
    from arix.core.crypto import encrypt_json

    channel = (
        await session.execute(
            select(Channel).where(Channel.tenant_id == tenant_id, Channel.type == channel_type)
        )
    ).scalars().first()
    if channel is None:
        channel = Channel(tenant_id=tenant_id, type=channel_type, settings={})
        session.add(channel)
    if credentials:
        channel.credentials_enc = encrypt_json(credentials)
    if settings:
        channel.settings = {**(channel.settings or {}), **settings}
    if external_id:
        channel.external_id = external_id
    channel.status = status
    await session.flush()
    return channel


async def ensure_staff(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    role: str,
    name: str,
    tg_user_id: int | None = None,
    email: str | None = None,
    password: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> Staff:
    stmt = select(Staff).where(Staff.tenant_id == tenant_id, Staff.role == role)
    if tg_user_id:
        stmt = stmt.where(Staff.tg_user_id == tg_user_id)
    elif email:
        stmt = stmt.where(Staff.email == email)
    member = (await session.execute(stmt)).scalars().first()
    if member is None:
        member = Staff(tenant_id=tenant_id, role=role, name=name)
        session.add(member)
    member.name = name
    if tg_user_id:
        member.tg_user_id = tg_user_id
    if email:
        member.email = email
    if entity_id:
        member.entity_id = entity_id
    if password:
        from arix.core.passwords import hash_password

        member.password_hash = hash_password(password)
    member.active = True
    await session.flush()
    return member


async def seed_demo_data(
    session: AsyncSession, ctx: TenantContext, *, days: int = 3
) -> dict[str, int]:
    """Create a handful of realistic visits so a demo panel is not empty."""
    from arix.db.models_core import Contact, Conversation
    from arix.verticals.clinic import booking as bk

    services = list(
        (await session.execute(select(Service).where(Service.tenant_id == ctx.id))).scalars()
    )
    if not services:
        return {"visits": 0}

    names = ["Ержан К.", "Мадина С.", "Алия Т.", "Данияр О.", "Асель Ж."]
    created = 0
    for index, name in enumerate(names):
        contact = Contact(
            tenant_id=ctx.id,
            phone=f"+7700000{index:04d}",
            name=name,
            lang=ctx.default_lang,
            source="import",
            consent_pd_at=now_utc(),
        )
        session.add(contact)
        await session.flush()
        session.add(
            Conversation(
                tenant_id=ctx.id,
                contact_id=contact.id,
                channel_type="tg",
                peer_id=f"demo-{contact.id}",
                state="active",
            )
        )
        service = services[index % len(services)]
        day = now_utc() + timedelta(days=(index % days) + 1)
        start = day.replace(hour=4 + index, minute=0, second=0, microsecond=0)
        offers = await bk.find_slots(
            session, ctx, service_id=service.id, count=1, now=start - timedelta(hours=2)
        )
        if not offers:
            continue
        offer = offers[0]
        try:
            visit = await bk.hold_slot(
                session, ctx,
                contact_id=contact.id, doctor_id=offer.doctor_id, service_id=service.id,
                start=offer.start, source="import", attribution="organic",
                idempotency_key=f"demo:{contact.id}",
            )
        except bk.SlotTaken:
            continue
        await bk.confirm_visit(session, ctx, visit.id)
        created += 1
    await session.flush()
    return {"visits": created}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base or {})
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


__all__ = [
    "ProvisionResult",
    "apply_template",
    "create_tenant",
    "ensure_channel",
    "ensure_staff",
    "publish_config",
    "seed_demo_data",
]
