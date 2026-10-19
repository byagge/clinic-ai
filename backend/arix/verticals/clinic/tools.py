"""Clinic tools (ТЗ §6.2).

Each tool returns plain JSON-able data.  Prices, durations, doctor names and
slot times come straight from the database — the model only formats them.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from arix.agent.toolkit import OutboundIntent, ToolContext, ToolDef, obj, tool
from arix.core.events import (
    BOOKING_CANCELLED,
    BOOKING_CONFIRMED,
    BOOKING_CREATED,
    BOOKING_RESCHEDULED,
    log_event,
)
from arix.core.timeutil import humanize_local, local_date, to_tenant
from arix.templates import render

from . import booking as bk
from .models import Service


def _service_payload(svc: Service, lang: str) -> dict:
    return {
        "service_id": str(svc.id),
        "name": svc.label(lang),
        "category": svc.category,
        "price": bk.service_price_text(svc, lang),
        "price_from": float(svc.price_from) if svc.price_from is not None else None,
        "price_to": float(svc.price_to) if svc.price_to is not None else None,
        "currency": svc.currency,
        "duration_min": svc.duration_min,
        "online_bookable": svc.online_bookable,
        "needs_consultation_first": svc.needs_consultation_first,
    }


@tool(
    "list_services",
    "Прайс и услуги центра: название, цена от-до, длительность. ОБЯЗАТЕЛЬНО вызывай, когда клиент "
    "спрашивает про цену, стоимость или что вы делаете. Называй только те цены, которые вернул "
    "этот инструмент.",
    obj(
        {
            "query": {"type": "string", "description": "Слова клиента: «чистка», «импланты»"},
            "category": {"type": "string"},
        }
    ),
    provides_prices=True,
)
async def list_services_tool(tc: ToolContext, args: dict) -> dict:
    services, matched = await bk.list_services(
        tc.session, tc.tenant_id, category=args.get("category"), query=args.get("query"),
        with_match_flag=True,
    )
    payload = [_service_payload(s, tc.lang) for s in services]
    tc.turn_state["services"] = {str(s.id): s for s in services}
    not_online = [p["name"] for p in payload if not p["online_bookable"]]
    return {
        "count": len(payload),
        # False means the query matched nothing and the list is just the full
        # price list — do NOT quote these prices as the answer.
        "query_matched": matched,
        "services": payload,
        "hint": (
            f"Услуги {', '.join(not_online)} онлайн не записываются — после выбора вызови handoff "
            "с reason=not_online_bookable."
            if not_online
            else None
        ),
    }


@tool(
    "route_by_complaint",
    "Подобрать 1-3 услуги или специальности по жалобе клиента. Это НЕ диагноз: инструмент только "
    "маршрутизирует. Никогда не называй болезнь сам.",
    obj({"complaint_text": {"type": "string"}}, ["complaint_text"]),
)
async def route_by_complaint_tool(tc: ToolContext, args: dict) -> dict:
    services = await bk.route_by_complaint(tc.session, tc.tenant_id, args["complaint_text"])
    return {
        "services": [_service_payload(s, tc.lang) for s in services],
        "disclaimer": "Это маршрутизация по справочнику, не диагноз. Диагноз ставит врач на приёме.",
    }


@tool(
    "list_doctors",
    "Врачи центра: имя, специальность, языки. Опционально — только те, кто делает услугу.",
    obj({"service_id": {"type": "string"}}),
)
async def list_doctors_tool(tc: ToolContext, args: dict) -> dict:
    service_id = _as_uuid(args.get("service_id"))
    doctors = await bk.list_doctors(tc.session, tc.tenant_id, service_id=service_id)
    return {
        "doctors": [
            {
                "doctor_id": str(d.id),
                "name": d.name,
                "specialty": d.specialty,
                "langs": list(d.langs or []),
                "bio": d.bio_short,
            }
            for d in doctors
        ]
    }


FIND_SLOTS_SCHEMA = obj(
    {
        "service_id": {"type": "string"},
        "doctor_id": {"type": "string"},
        "date_from": {"type": "string", "description": "YYYY-MM-DD, по умолчанию сегодня"},
        "date_to": {"type": "string"},
        "part_of_day": {"type": "string", "enum": ["morning", "day", "evening"]},
        "count": {"type": "integer", "description": "Сколько слотов предложить, по умолчанию 3"},
    },
    ["service_id"],
)


@tool(
    "find_slots",
    "Свободное время для записи. Возвращает слоты с ключом slot_key — именно его передавай в "
    "hold_slot. Предлагай клиенту 2-3 конкретных варианта, а не «когда вам удобно».",
    FIND_SLOTS_SCHEMA,
)
async def find_slots_tool(tc: ToolContext, args: dict) -> dict:
    service_id = _as_uuid(args.get("service_id"))
    if service_id is None:
        return {"error": "service_id_required", "slots": []}
    offers = await bk.find_slots(
        tc.session,
        tc.ctx,
        service_id=service_id,
        doctor_id=_as_uuid(args.get("doctor_id")),
        date_from=_as_date(args.get("date_from")),
        date_to=_as_date(args.get("date_to")),
        part_of_day=args.get("part_of_day"),
        count=int(args.get("count") or tc.ctx.settings.agent.slots_to_offer),
        now=tc.now,
    )
    tc.turn_state.setdefault("slot_offers", {}).update({o.key: o for o in offers})
    return {
        "count": len(offers),
        "slots": [
            {
                "slot_key": o.key,
                "when": humanize_local(o.start, tc.ctx.tz_name, tc.lang),
                "date": to_tenant(o.start, tc.ctx.tz_name).strftime("%Y-%m-%d"),
                "time": to_tenant(o.start, tc.ctx.tz_name).strftime("%H:%M"),
                "doctor": o.doctor_name,
                "doctor_id": str(o.doctor_id),
                "service": o.service_name,
            }
            for o in offers
        ],
        "note": "Свободных слотов нет в этом окне — предложи другие даты или add_to_waitlist."
        if not offers
        else None,
    }


@tool(
    "hold_slot",
    "Придержать выбранный слот на 10 минут, пока уточняешь имя, телефон и согласие на ПД. "
    "Передавай slot_key из find_slots.",
    obj({"slot_key": {"type": "string"}}, ["slot_key"]),
    mutating=True,
)
async def hold_slot_tool(tc: ToolContext, args: dict) -> dict:
    key = args.get("slot_key") or ""
    try:
        doctor_id, service_id, start = bk.SlotOffer.parse_key(key)
    except (ValueError, AttributeError):
        return {"ok": False, "error": "bad_slot_key", "hint": "Вызови find_slots снова."}

    service = await bk.get_service(tc.session, tc.tenant_id, service_id)
    if service is None:
        return {"ok": False, "error": "unknown_service"}
    if not service.online_bookable:
        tc.request_handoff(
            "not_online_bookable", "normal", f"Запись на {service.label(tc.lang)} только через админа"
        )
        return {"ok": False, "error": "not_online_bookable"}

    try:
        visit = await bk.hold_slot(
            tc.session,
            tc.ctx,
            contact_id=tc.contact.id,
            doctor_id=doctor_id,
            service_id=service_id,
            start=start,
            source="bot",
            attribution=_attribution(tc),
            ttl_minutes=10,
            idempotency_key=f"hold:{tc.conversation.id}:{key}",
        )
    except bk.SlotTaken:
        return {
            "ok": False,
            "error": "slot_taken",
            "hint": "Слот только что заняли. Вызови find_slots и предложи другое время.",
        }

    await log_event(
        tc.session,
        tc.tenant_id,
        BOOKING_CREATED,
        contact_id=tc.contact.id,
        conversation_id=tc.conversation.id,
        entity_ref=str(visit.id),
        source=visit.attribution,
        data={"status": "hold", "service": service.label("ru")},
    )
    needs = [
        field
        for field, present in (
            ("name", bool(tc.contact.name)),
            ("phone", bool(tc.contact.phone)),
            ("consent_pd", tc.contact.consent_pd_at is not None),
        )
        if not present
    ]
    return {
        "ok": True,
        "visit_id": str(visit.id),
        "expires_in_min": 10,
        "still_needed": needs,
        "next": "Спроси по одному только то, что в still_needed, затем вызови confirm_booking."
        if needs
        else "Вызови confirm_booking.",
    }


@tool(
    "confirm_booking",
    "Подтвердить запись после того, как известны имя, телефон и согласие на ПД. "
    "Возвращает готовый текст подтверждения с адресом и памяткой — отправь его клиенту.",
    obj(
        {
            "visit_id": {"type": "string"},
            "name": {"type": "string"},
            "phone": {"type": "string"},
            "consent_pd": {"type": "boolean"},
        },
        ["visit_id"],
    ),
    mutating=True,
)
async def confirm_booking_tool(tc: ToolContext, args: dict) -> dict:
    from arix.core.pii import normalize_phone

    visit_id = _as_uuid(args.get("visit_id"))
    if visit_id is None:
        return {"ok": False, "error": "bad_visit_id"}

    if args.get("name"):
        tc.contact.name = tc.vault.restore(args["name"])[:160]
    if args.get("phone"):
        phone = normalize_phone(tc.vault.restore(args["phone"]), tc.ctx.tenant.country)
        if phone:
            tc.contact.phone = phone
    if args.get("consent_pd") and tc.contact.consent_pd_at is None:
        from arix.core.timeutil import now_utc

        tc.contact.consent_pd_at = now_utc()

    missing = [
        f
        for f, ok in (
            ("name", bool(tc.contact.name)),
            ("phone", bool(tc.contact.phone)),
            ("consent_pd", tc.contact.consent_pd_at is not None),
        )
        if not ok
    ]
    if missing:
        return {
            "ok": False,
            "error": "missing_fields",
            "missing": missing,
            "hint": "Спроси по одному и вызови confirm_booking снова.",
        }

    visit = await bk.confirm_visit(tc.session, tc.ctx, visit_id)
    if visit is None:
        return {"ok": False, "error": "unknown_visit"}
    if visit.status == "cancelled":
        return {
            "ok": False,
            "error": "hold_expired",
            "hint": "Бронь истекла. Вызови find_slots и предложи время заново.",
        }

    refs = await bk.visit_with_refs(tc.session, tc.tenant_id, visit.id)
    assert refs is not None
    _, doctor, service, _ = refs

    prep = ""
    if service.prep_kb_id:
        from sqlalchemy import select

        from arix.db.models_core import KBItem

        item = (
            await tc.session.execute(
                select(KBItem).where(
                    KBItem.tenant_id == tc.tenant_id, KBItem.id == service.prep_kb_id
                )
            )
        ).scalar_one_or_none()
        if item:
            prep = f"Памятка: {item.answer}\n"

    s = tc.ctx.settings
    text = render(
        "booking_confirmed",
        tc.lang,
        overrides=s.texts,
        service=service.label(tc.lang),
        when=humanize_local(visit.starts_at, tc.ctx.tz_name, tc.lang),
        doctor=doctor.name,
        address=s.address or "уточните у администратора",
        map_line=f"Как добраться: {s.map_url}\n" if s.map_url else "",
        prep_line=prep,
        name=tc.contact.name or "",
    )

    await log_event(
        tc.session,
        tc.tenant_id,
        BOOKING_CONFIRMED,
        contact_id=tc.contact.id,
        conversation_id=tc.conversation.id,
        entity_ref=str(visit.id),
        amount=float(visit.price_quoted) if visit.price_quoted else None,
        currency=service.currency,
        source=visit.attribution,
        data={"service": service.label("ru"), "doctor": doctor.name},
    )
    tc.intents.append(OutboundIntent("visit_booked", {"visit_id": str(visit.id)}))
    return {
        "ok": True,
        "visit_id": str(visit.id),
        "confirmation_text": text,
        "instruction": "Отправь confirmation_text клиенту как есть, ничего не добавляя к цифрам.",
    }


@tool(
    "my_visits",
    "Будущие записи этого клиента: когда, к кому, на что.",
    obj({}),
)
async def my_visits_tool(tc: ToolContext, args: dict) -> dict:
    visits = await bk.upcoming_visits(tc.session, tc.tenant_id, tc.contact.id)
    out = []
    for v in visits:
        refs = await bk.visit_with_refs(tc.session, tc.tenant_id, v.id)
        if not refs:
            continue
        _, doctor, service, _ = refs
        out.append(
            {
                "visit_id": str(v.id),
                "when": humanize_local(v.starts_at, tc.ctx.tz_name, tc.lang),
                "doctor": doctor.name,
                "service": service.label(tc.lang),
                "status": v.status,
            }
        )
    return {"count": len(out), "visits": out}


@tool(
    "reschedule",
    "Перенести запись на другое время. Сначала найди новое время через find_slots.",
    obj({"visit_id": {"type": "string"}, "slot_key": {"type": "string"}}, ["visit_id", "slot_key"]),
    mutating=True,
)
async def reschedule_tool(tc: ToolContext, args: dict) -> dict:
    visit_id = _as_uuid(args.get("visit_id"))
    try:
        doctor_id, _service_id, start = bk.SlotOffer.parse_key(args.get("slot_key", ""))
    except (ValueError, AttributeError):
        return {"ok": False, "error": "bad_slot_key"}
    if visit_id is None:
        return {"ok": False, "error": "bad_visit_id"}
    try:
        moved = await bk.reschedule(tc.session, tc.ctx, visit_id, start, new_doctor_id=doctor_id)
    except bk.SlotTaken:
        return {"ok": False, "error": "slot_taken", "hint": "Предложи другое время."}
    except ValueError:
        return {"ok": False, "error": "unknown_visit"}

    refs = await bk.visit_with_refs(tc.session, tc.tenant_id, moved.id)
    assert refs is not None
    _, doctor, service, _ = refs
    await log_event(
        tc.session,
        tc.tenant_id,
        BOOKING_RESCHEDULED,
        contact_id=tc.contact.id,
        conversation_id=tc.conversation.id,
        entity_ref=str(moved.id),
        source=moved.attribution,
    )
    tc.intents.append(
        OutboundIntent("visit_booked", {"visit_id": str(moved.id), "rescheduled_from": str(visit_id)})
    )
    return {
        "ok": True,
        "visit_id": str(moved.id),
        "confirmation_text": render(
            "booking_rescheduled",
            tc.lang,
            overrides=tc.ctx.settings.texts,
            when=humanize_local(moved.starts_at, tc.ctx.tz_name, tc.lang),
            doctor=doctor.name,
            service=service.label(tc.lang),
            address=tc.ctx.settings.address or "",
        ),
    }


@tool(
    "cancel",
    "Отменить запись клиента.",
    obj({"visit_id": {"type": "string"}, "reason": {"type": "string"}}, ["visit_id"]),
    mutating=True,
)
async def cancel_tool(tc: ToolContext, args: dict) -> dict:
    visit_id = _as_uuid(args.get("visit_id"))
    if visit_id is None:
        return {"ok": False, "error": "bad_visit_id"}
    visit = await bk.cancel_visit(tc.session, tc.ctx, visit_id, args.get("reason") or "patient")
    if visit is None:
        return {"ok": False, "error": "unknown_visit"}
    await log_event(
        tc.session,
        tc.tenant_id,
        BOOKING_CANCELLED,
        contact_id=tc.contact.id,
        conversation_id=tc.conversation.id,
        entity_ref=str(visit.id),
        data={"reason": args.get("reason")},
    )
    tc.intents.append(OutboundIntent("visit_cancelled", {"visit_id": str(visit.id)}))
    return {
        "ok": True,
        "text": render(
            "booking_cancelled",
            tc.lang,
            overrides=tc.ctx.settings.texts,
            when=humanize_local(visit.starts_at, tc.ctx.tz_name, tc.lang),
        ),
    }


@tool(
    "add_to_waitlist",
    "Поставить клиента в лист ожидания, если в нужном окне нет свободных слотов.",
    obj(
        {
            "service_id": {"type": "string"},
            "doctor_id": {"type": "string"},
            "date_from": {"type": "string"},
            "date_to": {"type": "string"},
            "part_of_day": {"type": "string", "enum": ["morning", "day", "evening"]},
        },
        ["service_id"],
    ),
    mutating=True,
)
async def add_to_waitlist_tool(tc: ToolContext, args: dict) -> dict:
    if not tc.ctx.settings.features.waitlist:
        tc.request_handoff("other", "normal", "Клиент хочет лист ожидания, функция выключена")
        return {"ok": False, "error": "waitlist_disabled"}
    from .models import Waitlist

    service_id = _as_uuid(args.get("service_id"))
    if service_id is None:
        return {"ok": False, "error": "service_id_required"}
    today = local_date(tc.now, tc.ctx.tz_name)
    entry = Waitlist(
        tenant_id=tc.tenant_id,
        contact_id=tc.contact.id,
        service_id=service_id,
        doctor_id=_as_uuid(args.get("doctor_id")),
        date_from=_as_date(args.get("date_from")) or today,
        date_to=_as_date(args.get("date_to")) or today + timedelta(days=30),
        part_of_day=args.get("part_of_day"),
    )
    tc.session.add(entry)
    await tc.session.flush()
    return {"ok": True, "waitlist_id": str(entry.id)}


def _as_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _attribution(tc: ToolContext) -> str:
    """Where this booking came from — drives the revenue-share report."""
    scratch = tc.conversation.scratch or {}
    if scratch.get("attribution"):
        return str(scratch["attribution"])
    local_hour = to_tenant(tc.now, tc.ctx.tz_name).hour
    quiet = tc.ctx.settings.quiet
    if quiet and quiet.contains_local(to_tenant(tc.now, tc.ctx.tz_name)):
        return "bot_night"
    if local_hour < 9 or local_hour >= 21:
        return "bot_night"
    return "bot"


CLINIC_TOOLS: list[ToolDef] = [
    list_services_tool,
    route_by_complaint_tool,
    list_doctors_tool,
    find_slots_tool,
    hold_slot_tool,
    confirm_booking_tool,
    my_visits_tool,
    reschedule_tool,
    cancel_tool,
    add_to_waitlist_tool,
]

__all__ = ["CLINIC_TOOLS"]
