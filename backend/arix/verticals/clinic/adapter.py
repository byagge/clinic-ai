"""Clinic implementation of the runtime's :class:`VerticalAdapter`."""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from arix.agent.runtime import ButtonOutcome, ConversationRuntime
from arix.agent.toolkit import ToolRegistry
from arix.agent.tools_core import CORE_TOOLS, PAYMENT_TOOLS
from arix.core.context import TenantContext
from arix.core.timeutil import humanize_local, to_tenant
from arix.db.models_core import Contact, Conversation
from arix.domain.notify import notify_staff
from arix.logging_setup import get_logger
from arix.scheduler import jobs

from . import booking as bk
from .playbook import ClinicPlaybook
from .scheduling import cancel_visit_jobs, schedule_visit_jobs
from .tools import CLINIC_TOOLS

log = get_logger("arix.clinic")


class ClinicVertical:
    name = "clinic"

    def playbook(self):
        return ClinicPlaybook()

    async def handle_button(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        contact: Contact,
        data: str,
        lang: str,
    ) -> ButtonOutcome | None:
        if data.startswith("slot:"):
            return await self._book_slot(session, ctx, conversation, contact, data[5:], lang)
        if data.startswith("visit:confirm:"):
            visit_id = _uuid(data.rsplit(":", 1)[-1])
            if visit_id is None:
                return None
            await bk.set_status(session, ctx, visit_id, "confirmed")
            await jobs.cancel(session, ctx.id, dedupe_key=jobs.dedupe("call_task", visit_id))
            return ButtonOutcome(reply="Спасибо, ждём вас!", label="visit_confirmed")
        if data.startswith("visit:cancel:"):
            visit_id = _uuid(data.rsplit(":", 1)[-1])
            if visit_id is None:
                return None
            visit = await bk.cancel_visit(session, ctx, visit_id, "patient_button")
            await cancel_visit_jobs(session, ctx, visit_id)
            if visit is not None:
                await notify_staff(
                    session, ctx, roles=["admin"],
                    text=(
                        f"🚫 Отмена записи\n{contact.name or 'без имени'}"
                        f"\n{to_tenant(visit.starts_at, ctx.tz_name):%d.%m %H:%M}"
                    ),
                )
            return ButtonOutcome(
                reply="Отменила запись. Подобрать другое время?", label="visit_cancelled"
            )
        if data.startswith("visit:reschedule:"):
            visit_id = _uuid(data.rsplit(":", 1)[-1])
            if visit_id is None:
                return None
            scratch = dict(conversation.scratch or {})
            scratch["reschedule_visit_id"] = str(visit_id)
            conversation.scratch = scratch
            await session.flush()
            # Fall through to the agent so it offers new slots.
            return ButtonOutcome(handled=False, label="reschedule_start")
        if data.startswith("review:"):
            from .reviews import record_review

            try:
                _, raw_visit, raw_score = data.split(":")
            except ValueError:
                return None
            visit_id = _uuid(raw_visit)
            if visit_id is None:
                return None
            await record_review(session, ctx, visit_id, int(raw_score), conversation)
            return ButtonOutcome(label="review_recorded")
        return None

    async def _book_slot(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        contact: Contact,
        slot_key: str,
        lang: str,
    ) -> ButtonOutcome:
        scratch = dict(conversation.scratch or {})
        reschedule_id = scratch.get("reschedule_visit_id")
        try:
            doctor_id, service_id, start = bk.SlotOffer.parse_key(slot_key)
        except (ValueError, AttributeError):
            return ButtonOutcome(
                reply="Это время уже неактуально. Подобрать новое?", label="bad_slot"
            )

        if reschedule_id:
            try:
                moved = await bk.reschedule(
                    session, ctx, uuid.UUID(reschedule_id), start, new_doctor_id=doctor_id
                )
            except bk.SlotTaken:
                return ButtonOutcome(
                    reply="Это время только что заняли. Подобрать другое?", label="slot_taken"
                )
            except ValueError:
                return ButtonOutcome(reply="Не нашла эту запись.", label="unknown_visit")
            scratch.pop("reschedule_visit_id", None)
            scratch["offered_slots"] = []
            conversation.scratch = scratch
            await schedule_visit_jobs(session, ctx, moved.id)
            refs = await bk.visit_with_refs(session, ctx.id, moved.id)
            doctor_name = refs[1].name if refs else ""
            return ButtonOutcome(
                reply=f"Перенесла на {humanize_local(moved.starts_at, ctx.tz_name, lang)}"
                + (f", врач {doctor_name}." if doctor_name else "."),
                label="rescheduled",
            )

        try:
            visit = await bk.hold_slot(
                session, ctx,
                contact_id=contact.id, doctor_id=doctor_id, service_id=service_id, start=start,
                source="bot", attribution="bot",
                idempotency_key=f"btn:{conversation.id}:{slot_key}",
            )
        except bk.SlotTaken:
            return ButtonOutcome(
                reply="Это время только что заняли. Подобрать другое?", label="slot_taken"
            )

        missing = [
            field
            for field, ok in (
                ("name", bool(contact.name)),
                ("phone", bool(contact.phone)),
                ("consent_pd", contact.consent_pd_at is not None),
            )
            if not ok
        ]
        scratch["pending_visit_id"] = str(visit.id)
        scratch["still_needed"] = missing
        scratch["offered_slots"] = []
        conversation.scratch = scratch
        await session.flush()

        if missing:
            from arix.agent.scripted import ask_for

            return ButtonOutcome(reply=ask_for(missing), label="slot_held")

        confirmed = await bk.confirm_visit(session, ctx, visit.id)
        if confirmed is None:
            return ButtonOutcome(reply="Не получилось подтвердить запись.", label="error")
        scratch["pending_visit_id"] = None
        conversation.scratch = scratch
        await self.on_conversion(
            session, ctx, conversation, "visit_booked", {"visit_id": str(visit.id)}
        )
        refs = await bk.visit_with_refs(session, ctx.id, visit.id)
        if refs is None:
            return ButtonOutcome(reply="Записала вас.", label="booked")
        _, doctor, service, _ = refs
        from arix.templates import render

        return ButtonOutcome(
            reply=render(
                "booking_confirmed", lang, overrides=ctx.settings.texts,
                service=service.label(lang),
                when=humanize_local(visit.starts_at, ctx.tz_name, lang),
                doctor=doctor.name,
                address=ctx.settings.address or "уточните у администратора",
                map_line=f"Как добраться: {ctx.settings.map_url}\n" if ctx.settings.map_url else "",
                prep_line="",
            ),
            label="booked",
        )

    async def on_conversion(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        kind: str,
        payload: dict,
    ) -> None:
        visit_id = _uuid(payload.get("visit_id"))
        if visit_id is None:
            return
        if kind == "visit_cancelled":
            await cancel_visit_jobs(session, ctx, visit_id)
            return
        await schedule_visit_jobs(session, ctx, visit_id)
        refs = await bk.visit_with_refs(session, ctx.id, visit_id)
        if refs is None:
            return
        visit, doctor, service, contact = refs
        await notify_staff(
            session, ctx, roles=["admin"],
            text=(
                f"✅ Новая запись\n{contact.name or 'без имени'}"
                + (f" · {contact.phone}" if contact.phone else "")
                + f"\n{service.label('ru')} · "
                f"{to_tenant(visit.starts_at, ctx.tz_name):%d.%m %H:%M}"
                f"\nВрач: {doctor.name}\nИсточник: {visit.attribution}"
            ),
        )


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.extend(CORE_TOOLS)
    registry.extend(CLINIC_TOOLS)
    registry.extend(PAYMENT_TOOLS)
    return registry


def build_runtime(brain=None) -> ConversationRuntime:
    return ConversationRuntime(build_registry(), ClinicVertical(), brain=brain)


def _uuid(value) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


__all__ = ["ClinicVertical", "build_registry", "build_runtime"]
