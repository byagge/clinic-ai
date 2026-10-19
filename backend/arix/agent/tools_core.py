"""Tools available in every vertical."""
from __future__ import annotations

from arix.core.events import log_event
from arix.core.pii import normalize_phone
from arix.core.timeutil import now_utc
from arix.kb.search import search_kb

from .toolkit import ToolContext, ToolDef, obj, tool

KB_SCHEMA = obj(
    {
        "query": {"type": "string", "description": "Что именно спросил клиент, своими словами"},
        "lang": {"type": "string", "enum": ["ru", "kz", "uz"]},
        "types": {
            "type": "array",
            "items": {"type": "string", "enum": ["faq", "policy", "prep", "address", "other"]},
        },
    },
    ["query"],
)


@tool(
    "kb_search",
    "Найти факт в базе знаний центра: адрес, часы работы, условия, подготовка к процедуре, "
    "политика отмены и возврата, документы. Вызывай ВСЕГДА, когда клиент спрашивает о чём-то, "
    "чего нет в других инструментах. Факты с флагом verbatim цитируй дословно.",
    KB_SCHEMA,
    provides_prices=True,
)
async def kb_search_tool(tc: ToolContext, args: dict) -> dict:
    hits = await search_kb(
        tc.session,
        tc.tenant_id,
        args.get("query", ""),
        lang=args.get("lang") or tc.lang,
        types=args.get("types"),
        limit=5,
    )
    return {
        "found": len(hits),
        "facts": [h.as_fact() for h in hits],
        "note": "Если facts пустой — скажи клиенту, что уточнишь, и вызови handoff."
        if not hits
        else None,
    }


HANDOFF_SCHEMA = obj(
    {
        "reason": {
            "type": "string",
            "enum": [
                "patient_request",
                "red_flag",
                "complaint",
                "discount",
                "refund",
                "out_of_kb",
                "media",
                "not_online_bookable",
                "confusion",
                "other",
            ],
        },
        "priority": {"type": "string", "enum": ["normal", "high", "urgent"]},
        "summary": {
            "type": "string",
            "description": "1-2 строки для администратора: что хочет клиент и что уже сделано",
        },
    },
    ["reason"],
)


@tool(
    "handoff",
    "Передать диалог живому администратору. Вызывай при просьбе человека, жалобе, конфликте, "
    "красном флаге, торге/скидке, возврате денег, вопросе вне базы, фото и документах. "
    "После вызова напиши клиенту короткую фразу, что подключается администратор.",
    HANDOFF_SCHEMA,
    terminal=False,
    mutating=True,
)
async def handoff_tool(tc: ToolContext, args: dict) -> dict:
    reason = args.get("reason", "other")
    priority = args.get("priority") or ("urgent" if reason == "red_flag" else "normal")
    tc.request_handoff(reason, priority, args.get("summary"))
    return {"ok": True, "reason": reason, "priority": priority}


SAVE_CONTACT_SCHEMA = obj(
    {
        "name": {"type": "string"},
        "phone": {"type": "string"},
        "lang": {"type": "string", "enum": ["ru", "kz", "uz"]},
        "consent_pd": {"type": "boolean", "description": "Клиент явно согласился на обработку ПД"},
        "consent_marketing": {"type": "boolean"},
    }
)


@tool(
    "save_contact_fields",
    "Сохранить имя, телефон, язык или согласие клиента. Вызывай сразу, как клиент их назвал. "
    "Не спрашивай данные, которые уже сохранены.",
    SAVE_CONTACT_SCHEMA,
    mutating=True,
)
async def save_contact_tool(tc: ToolContext, args: dict) -> dict:
    contact = tc.contact
    changed: list[str] = []
    # Values arriving from the model may be PII tokens — restore them first.
    name = tc.vault.restore(args.get("name") or "") or None
    phone_raw = tc.vault.restore(args.get("phone") or "") or None
    if name and name != contact.name:
        contact.name = name[:160]
        changed.append("name")
    if phone_raw:
        phone = normalize_phone(phone_raw, tc.ctx.tenant.country)
        if phone and phone != contact.phone:
            contact.phone = phone
            changed.append("phone")
    lang = args.get("lang")
    if lang and lang in tc.ctx.languages and lang != contact.lang:
        contact.lang = lang
        changed.append("lang")
    if args.get("consent_pd") and contact.consent_pd_at is None:
        contact.consent_pd_at = now_utc()
        changed.append("consent_pd")
    if args.get("consent_marketing") and contact.consent_marketing_at is None:
        contact.consent_marketing_at = now_utc()
        changed.append("consent_marketing")
    if changed:
        await tc.session.flush()
        await log_event(
            tc.session,
            tc.tenant_id,
            "contact_updated",
            contact_id=contact.id,
            conversation_id=tc.conversation.id,
            data={"fields": changed},
        )
    return {
        "saved": changed,
        "known": {
            "name": bool(contact.name),
            "phone": bool(contact.phone),
            "consent_pd": contact.consent_pd_at is not None,
        },
    }


@tool(
    "get_contact_state",
    "Что уже известно о клиенте: имя, телефон, согласие, язык, прошлые визиты. "
    "Вызови прежде чем спрашивать имя или телефон.",
    obj({}),
)
async def contact_state_tool(tc: ToolContext, args: dict) -> dict:
    c = tc.contact
    return {
        "has_name": bool(c.name),
        "name_token": tc.vault.token_for("NAME", c.name) if c.name else None,
        "has_phone": bool(c.phone),
        "consent_pd": c.consent_pd_at is not None,
        "consent_marketing": c.consent_marketing_at is not None,
        "lang": c.lang,
        "is_returning": c.last_visit_at is not None,
    }


PAYMENT_SCHEMA = obj(
    {
        "amount": {"type": "number"},
        "entity_ref": {"type": "string", "description": "id визита или счёта"},
        "comment": {"type": "string"},
    },
    ["amount"],
)


@tool(
    "create_payment_link",
    "Создать ссылку на оплату (Kaspi / Click / Payme). Сумму бери только из других инструментов, "
    "никогда не придумывай.",
    PAYMENT_SCHEMA,
    provides_prices=True,
    mutating=True,
)
async def create_payment_link_tool(tc: ToolContext, args: dict) -> dict:
    from arix.payments import create_payment

    if not tc.ctx.settings.features.miniapp_payments:
        tc.request_handoff("other", "normal", "Клиент хочет оплатить, платежи не подключены")
        return {"ok": False, "error": "payments_disabled"}
    payment, url = await create_payment(
        tc.session,
        tc.ctx,
        contact_id=tc.contact.id,
        amount=float(args["amount"]),
        entity_ref=args.get("entity_ref"),
        attributed_to="miniapp",
    )
    return {"ok": True, "url": url, "amount": float(payment.amount), "currency": payment.currency}


CORE_TOOLS: list[ToolDef] = [
    kb_search_tool,
    contact_state_tool,
    save_contact_tool,
    handoff_tool,
]

PAYMENT_TOOLS: list[ToolDef] = [create_payment_link_tool]

__all__ = ["CORE_TOOLS", "PAYMENT_TOOLS"]
