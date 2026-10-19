"""Staff bots: admin, owner and doctor, all on one Telegram webhook.

A staff member is recognised by ``staff.tg_user_id``; the role in that row
decides what the command does.  Patient dialogues and staff dialogues never
mix because a staff member writes to the bot in their own private chat, while
patients write to the clinic's chat/business account.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.channels.base import InboundMessage
from arix.core.context import TenantContext
from arix.core.timeutil import combine_local, local_date, now_utc, to_tenant
from arix.db.models_core import Contact, Conversation, Event, Handoff, Staff
from arix.domain.handoff import handoff_card, open_handoffs, resolve_handoff, take_handoff
from arix.domain.notify import notify_one
from arix.logging_setup import get_logger
from arix.seed.provision import publish_config
from arix.verticals.clinic import booking as bk
from arix.verticals.clinic.models import Doctor, Service
from arix.verticals.clinic.reports import (
    build_daily_report,
    doctor_day_text,
    render_daily_report,
)
from arix.verticals.clinic.scheduling import schedule_after_visit

log = get_logger("arix.bots.staff")

HELP_ADMIN = """Команды администратора:
/today — приёмы и статус подтверждений на сегодня
/handoffs — открытые передачи
/lost — кто спросил и не записался
/pause_bot, /resume_bot — остановить или включить бота
/help — эта справка"""

HELP_OWNER = """Команды владельца:
/today — что сегодня
/week — неделя
/report — отчёт за вчера
/lost — потерянные обращения
/reviews — отзывы
/prices код цена_от [цена_до] — быстро поменять цену
/off Имя_врача ГГГГ-ММ-ДД — врач не работает в этот день
/pause_bot, /resume_bot
/help"""

HELP_DOCTOR = """Команды врача:
/today — ваше расписание на сегодня
/tomorrow — на завтра
Отмечайте «пришёл» и «не пришёл» кнопками под расписанием."""


async def find_staff(
    session: AsyncSession, ctx: TenantContext, tg_user_id: int | None
) -> Staff | None:
    if not tg_user_id:
        return None
    return (
        await session.execute(
            select(Staff).where(
                Staff.tenant_id == ctx.id,
                Staff.tg_user_id == tg_user_id,
                Staff.active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def handle_staff_update(
    session: AsyncSession, ctx: TenantContext, inbound: InboundMessage
) -> bool:
    """Returns ``True`` when the update was a staff action and was consumed."""
    if inbound.business_connection_id:
        return False  # business chats are patient-facing by definition
    user_id = int(inbound.from_user_id) if (inbound.from_user_id or "").isdigit() else None
    member = await find_staff(session, ctx, user_id)
    if member is None:
        return False

    if inbound.callback_data:
        await _handle_callback(session, ctx, member, inbound)
        return True

    text = (inbound.text or "").strip()
    if not text.startswith("/"):
        # Free text from staff in their own chat: treat as a note, not a command.
        await notify_one(
            session, ctx, member.tg_user_id,
            "Я понимаю только команды. /help — список.",
        )
        return True

    command, _, argument = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    argument = argument.strip()
    reply, buttons = await _run_command(session, ctx, member, command, argument)
    if reply:
        await notify_one(session, ctx, member.tg_user_id, reply, buttons)
    return True


async def _run_command(
    session: AsyncSession,
    ctx: TenantContext,
    member: Staff,
    command: str,
    argument: str,
) -> tuple[str | None, list[tuple[str, str]] | None]:
    role = member.role
    tz = ctx.tz_name
    today = local_date(now_utc(), tz)

    if command in ("/start", "/help"):
        return (
            {"owner": HELP_OWNER, "admin": HELP_ADMIN, "doctor": HELP_DOCTOR}.get(role, HELP_ADMIN),
            None,
        )

    if command == "/today":
        if role == "doctor" and member.entity_id:
            doctor = (
                await session.execute(
                    select(Doctor).where(Doctor.id == member.entity_id)
                )
            ).scalar_one_or_none()
            if doctor is None:
                return "Ваш профиль врача не найден — напишите администратору.", None
            text = await doctor_day_text(session, ctx, doctor, today)
            return text, await _doctor_buttons(session, ctx, doctor, today)
        return await _today_overview(session, ctx, today), None

    if command == "/tomorrow" and role == "doctor" and member.entity_id:
        doctor = (
            await session.execute(select(Doctor).where(Doctor.id == member.entity_id))
        ).scalar_one_or_none()
        if doctor is None:
            return "Ваш профиль врача не найден.", None
        return await doctor_day_text(session, ctx, doctor, today + timedelta(days=1)), None

    if command == "/week":
        return await _week_overview(session, ctx, today), None

    if command == "/report":
        report = await build_daily_report(session, ctx)
        return render_daily_report(ctx, report), None

    if command == "/handoffs":
        rows = await open_handoffs(session, ctx.id, limit=10)
        if not rows:
            return "Открытых передач нет.", None
        blocks = []
        for handoff in rows:
            card, _ = await handoff_card(session, ctx, handoff)
            blocks.append(card)
        return "\n\n".join(blocks), None

    if command == "/lost":
        return await _lost_report(session, ctx), None

    if command == "/reviews":
        return await _reviews_report(session, ctx), None

    if command in ("/pause_bot", "/resume_bot"):
        if role not in ("owner", "admin"):
            return "Эта команда доступна владельцу и администратору.", None
        paused = command == "/pause_bot"
        await publish_config(
            session, ctx.id, {"agent": {"paused": paused}}, note=f"{role}:{command}"
        )
        return ("Бот остановлен. Диалоги будут приходить вам." if paused
                else "Бот снова отвечает."), None

    if command == "/prices":
        if role != "owner":
            return "Менять цены может владелец.", None
        parts = argument.split()
        if len(parts) < 2:
            return "Формат: /prices код цена_от [цена_до]", None
        code, price_from, *rest = parts
        service = (
            await session.execute(
                select(Service).where(Service.tenant_id == ctx.id, Service.code == code)
            )
        ).scalar_one_or_none()
        if service is None:
            codes = (
                await session.execute(
                    select(Service.code).where(Service.tenant_id == ctx.id).limit(30)
                )
            ).scalars()
            return f"Услуги «{code}» нет. Доступны: {', '.join(codes)}", None
        try:
            service.price_from = float(price_from.replace(" ", ""))
            if rest:
                service.price_to = float(rest[0].replace(" ", ""))
        except ValueError:
            return "Цена должна быть числом.", None
        await session.flush()
        return (
            f"{service.label('ru')}: {bk.service_price_text(service)}",
            None,
        )

    if command == "/off":
        if role not in ("owner", "admin"):
            return "Эта команда доступна владельцу и администратору.", None
        parts = argument.rsplit(" ", 1)
        if len(parts) != 2:
            return "Формат: /off Имя_врача ГГГГ-ММ-ДД", None
        name, raw_date = parts
        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            return "Дата должна быть в формате ГГГГ-ММ-ДД.", None
        doctors = await bk.list_doctors(session, ctx.id)
        doctor = next(
            (d for d in doctors if name.strip().lower() in d.name.lower()), None
        )
        if doctor is None:
            return f"Врача «{name}» не нашла. Есть: " + ", ".join(d.name for d in doctors), None
        from arix.verticals.clinic.models import ScheduleException

        session.add(
            ScheduleException(
                tenant_id=ctx.id, doctor_id=doctor.id, date=day, type="off",
                note=f"set by {member.name}",
            )
        )
        await session.flush()
        day_start = combine_local(day, datetime.min.time(), tz)
        clashing = [
            v
            for v in await bk.visits_in_window(
                session, ctx.id, day_start, day_start + timedelta(days=1),
                statuses=["booked", "confirmed", "hold"],
            )
            if v.doctor_id == doctor.id
        ]
        tail = (
            f"\n⚠️ На этот день уже есть записей: {len(clashing)} — их нужно перенести вручную."
            if clashing
            else ""
        )
        return f"{doctor.name} не работает {day:%d.%m}.{tail}", None

    return "Не знаю такую команду. /help — список.", None


async def _handle_callback(
    session: AsyncSession, ctx: TenantContext, member: Staff, inbound: InboundMessage
) -> None:
    data = inbound.callback_data or ""
    if data.startswith("ho:"):
        _, action, raw_id = data.split(":", 2)
        handoff_id = uuid.UUID(raw_id)
        if action == "take":
            handoff = await take_handoff(session, ctx, handoff_id, member.id)
            if handoff is None:
                await notify_one(session, ctx, member.tg_user_id, "Передача не найдена.")
                return
            conversation = (
                await session.execute(
                    select(Conversation).where(Conversation.id == handoff.conversation_id)
                )
            ).scalar_one()
            contact = (
                await session.execute(select(Contact).where(Contact.id == conversation.contact_id))
            ).scalar_one()
            await notify_one(
                session, ctx, member.tg_user_id,
                f"Передача #{handoff.number} ваша. Бот больше не отвечает в этом диалоге.\n"
                f"Клиент: {contact.name or 'без имени'}"
                + (f" · {contact.phone}" if contact.phone else ""),
            )
        elif action == "bot":
            await resolve_handoff(session, ctx, handoff_id, back_to_bot=True)
            await notify_one(session, ctx, member.tg_user_id, "Вернула диалог боту.")
        elif action == "open":
            handoff = (
                await session.execute(select(Handoff).where(Handoff.id == handoff_id))
            ).scalar_one_or_none()
            if handoff is None:
                return
            from arix.domain.conversations import history

            rows = await history(session, handoff.conversation_id, limit=12)
            lines = [
                f"{'👤' if m.direction == 'in' else '🤖'} {(m.stt_text or m.text or '')[:200]}"
                for m in rows
            ]
            await notify_one(
                session, ctx, member.tg_user_id,
                "Последние сообщения:\n" + "\n".join(lines or ["пусто"]),
            )
        return

    if data.startswith("v:"):
        # Doctor marks attendance: v:arrived:<visit_id> / v:noshow:<visit_id>
        _, action, raw_id = data.split(":", 2)
        visit_id = uuid.UUID(raw_id)
        if action == "arrived":
            await bk.set_status(session, ctx, visit_id, "done")
            refs = await bk.visit_with_refs(session, ctx.id, visit_id)
            if refs:
                visit, _doctor, _service, contact = refs
                contact.last_visit_at = visit.starts_at
            await schedule_after_visit(session, ctx, visit_id)
            await notify_one(session, ctx, member.tg_user_id, "Отметила: пришёл.")
        elif action == "noshow":
            await bk.set_status(session, ctx, visit_id, "no_show")
            from arix.verticals.clinic.scheduling import (
                cancel_visit_jobs,
                schedule_noshow_rebook,
            )

            await cancel_visit_jobs(session, ctx, visit_id)
            await schedule_noshow_rebook(session, ctx, visit_id)
            await notify_one(
                session, ctx, member.tg_user_id,
                "Отметила: не пришёл. Бот предложит клиенту новое время через час.",
            )
        return

    if data.startswith("rep:"):
        kind = data.split(":", 1)[1]
        if kind == "today":
            text = await _today_overview(session, ctx, local_date(now_utc(), ctx.tz_name))
        elif kind == "lost":
            text = await _lost_report(session, ctx)
        elif kind == "reviews":
            text = await _reviews_report(session, ctx)
        else:
            text = "Не знаю такой отчёт."
        await notify_one(session, ctx, member.tg_user_id, text)


async def _doctor_buttons(
    session: AsyncSession, ctx: TenantContext, doctor: Doctor, day: date
) -> list[tuple[str, str]]:
    day_start = combine_local(day, datetime.min.time(), ctx.tz_name)
    visits = await bk.doctor_day(session, ctx.id, doctor.id, day_start)
    buttons: list[tuple[str, str]] = []
    for visit in visits:
        if visit.status not in ("booked", "confirmed", "arrived"):
            continue
        label = to_tenant(visit.starts_at, ctx.tz_name).strftime("%H:%M")
        buttons.append((f"✅ {label}", f"v:arrived:{visit.id}"))
        buttons.append((f"❌ {label}", f"v:noshow:{visit.id}"))
    return buttons[:10]


async def _today_overview(session: AsyncSession, ctx: TenantContext, day: date) -> str:
    tz = ctx.tz_name
    start = combine_local(day, datetime.min.time(), tz)
    rows = (
        await session.execute(
            select(bk.Visit, Doctor, Service, Contact)
            .join(Doctor, Doctor.id == bk.Visit.doctor_id)
            .join(Service, Service.id == bk.Visit.service_id)
            .join(Contact, Contact.id == bk.Visit.contact_id)
            .where(
                bk.Visit.tenant_id == ctx.id,
                bk.Visit.starts_at >= start,
                bk.Visit.starts_at < start + timedelta(days=1),
                bk.Visit.status.notin_(["cancelled"]),
            )
            .order_by(bk.Visit.starts_at)
        )
    ).all()
    if not rows:
        return f"{day:%d.%m}: записей нет."
    lines = [f"{day:%d.%m} · записей {len(rows)}"]
    for visit, doctor, service, contact in rows:
        mark = {
            "confirmed": "✔",
            "arrived": "✅",
            "done": "✅",
            "no_show": "❌",
            "hold": "⏳",
        }.get(visit.status, "•")
        lines.append(
            f"{mark} {to_tenant(visit.starts_at, tz):%H:%M} "
            f"{contact.name or 'без имени'} — {service.label('ru')} ({doctor.name})"
        )
    unconfirmed = [c.name or "без имени" for v, _d, _s, c in rows if v.status == "booked"]
    if unconfirmed:
        lines.append("Не подтвердили: " + ", ".join(unconfirmed[:8]))
    return "\n".join(lines)


async def _week_overview(session: AsyncSession, ctx: TenantContext, day: date) -> str:
    tz = ctx.tz_name
    start = combine_local(day, datetime.min.time(), tz)
    visits = await bk.visits_in_window(
        session, ctx.id, start, start + timedelta(days=7)
    )
    by_day: dict[date, int] = {}
    for visit in visits:
        if visit.status == "cancelled":
            continue
        key = to_tenant(visit.starts_at, tz).date()
        by_day[key] = by_day.get(key, 0) + 1
    if not by_day:
        return "На неделю записей нет."
    lines = ["Неделя:"]
    for offset in range(7):
        current = day + timedelta(days=offset)
        lines.append(f"{current:%a %d.%m}: {by_day.get(current, 0)}")
    return "\n".join(lines)


async def _lost_report(session: AsyncSession, ctx: TenantContext) -> str:
    since = now_utc() - timedelta(days=7)
    rows = (
        await session.execute(
            select(Event)
            .where(
                Event.tenant_id == ctx.id,
                Event.type == "interest",
                Event.created_at >= since,
            )
            .order_by(Event.created_at.desc())
            .limit(100)
        )
    ).scalars()
    interests = {row.contact_id: row for row in rows if row.contact_id}
    if not interests:
        return "За неделю потерянных обращений нет."
    lost: list[str] = []
    for contact_id, event in interests.items():
        if await bk.has_future_visit(session, ctx.id, contact_id):
            continue
        contact = (
            await session.execute(select(Contact).where(Contact.id == contact_id))
        ).scalar_one_or_none()
        if contact is None:
            continue
        topic = (event.data or {}).get("service") or "услуга не указана"
        lost.append(
            f"• {contact.name or 'без имени'}"
            + (f" · {contact.phone}" if contact.phone else "")
            + f" — {topic}"
        )
    if not lost:
        return "Все, кто интересовался за неделю, записались."
    return f"Потерянные за 7 дней ({len(lost)}):\n" + "\n".join(lost[:20])


async def _reviews_report(session: AsyncSession, ctx: TenantContext) -> str:
    from arix.verticals.clinic.models import Review

    since = now_utc() - timedelta(days=30)
    rows = list(
        (
            await session.execute(
                select(Review)
                .where(
                    Review.tenant_id == ctx.id,
                    Review.created_at >= since,
                    Review.score.isnot(None),
                )
                .order_by(Review.created_at.desc())
            )
        ).scalars()
    )
    if not rows:
        return "За месяц оценок нет."
    buckets: dict[int, int] = {}
    for review in rows:
        buckets[review.score] = buckets.get(review.score, 0) + 1
    average = sum(r.score for r in rows) / len(rows)
    lines = [
        f"Отзывы за 30 дней: {len(rows)}, средняя {average:.1f}",
        " · ".join(f"{score}★ ×{count}" for score, count in sorted(buckets.items(), reverse=True)),
    ]
    bad = [r for r in rows if r.score <= 3 and r.text]
    for review in bad[:5]:
        lines.append(f"{review.score}★: «{review.text[:200]}»")
    return "\n".join(lines)


__all__ = ["find_staff", "handle_staff_update"]
