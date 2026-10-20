"""Conversation runtime.

Pipeline for one inbound message:

    dedupe → contact/conversation → STT → precheck → (hard routes)
           → mask PII → build prompt+state → brain(tools) → postcheck
           → unmask → deliver → intents (handoff, jobs) → events

Hard routes bypass the model entirely: a red flag must never depend on an LLM
deciding to call a tool, an opt-out must be honoured instantly, and a message
in a conversation a human has taken over must not be answered by the bot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from arix.channels.base import Button, InboundMessage
from arix.config import get_settings
from arix.core.context import TenantContext
from arix.core.events import (
    FIRST_RESPONSE,
    INBOUND,
    INTEREST,
    OPT_OUT,
    RED_FLAG,
    TOOL_CALL,
    log_event,
)
from arix.core.pii import PIIVault, mask_text
from arix.core.timeutil import now_utc, to_tenant
from arix.db.models_core import Contact, Conversation
from arix.domain import conversations as convo
from arix.domain.handoff import create_handoff, handoff_card, staff_for_role
from arix.domain.messaging import send_message
from arix.domain.notify import notify_staff
from arix.logging_setup import get_logger
from arix.scheduler import jobs
from arix.templates import render

from .brain import Brain, BrainResult, build_brain
from .guardrails import Intent, postcheck, precheck
from .toolkit import ToolContext, ToolRegistry

log = get_logger("arix.runtime")

MAX_REPLY_CHARS = 1200


class VerticalAdapter(Protocol):
    """Everything the runtime needs that is specific to one vertical.

    Keeping this a protocol is what lets ``arix/agent``, ``arix/core``,
    ``arix/channels`` and ``arix/scheduler`` stay byte-identical between the
    clinic and edu deployments (see docs/CORE_SYNC.md).
    """

    name: str

    def playbook(self):
        """The deterministic dialogue flow for this vertical."""
        ...

    async def handle_button(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        contact: Contact,
        data: str,
        lang: str,
    ) -> ButtonOutcome | None: ...

    async def on_conversion(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        kind: str,
        payload: dict,
    ) -> None: ...


@dataclass
class ButtonOutcome:
    """What a vertical did with a button tap."""

    #: Text to send back, if any (the vertical may have sent it itself).
    reply: str | None = None
    #: ``True`` when the tap is fully handled and the model must not run.
    handled: bool = True
    label: str = "button"


@dataclass
class TurnResult:
    handled: bool
    reply: str | None = None
    tool_calls: list[str] = field(default_factory=list)
    handoff_created: bool = False
    skipped_reason: str | None = None
    guard_violations: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class ConversationRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        vertical: VerticalAdapter,
        *,
        brain: Brain | None = None,
    ) -> None:
        self.registry = registry
        self.vertical_adapter = vertical
        self.vertical = vertical.name
        self._brain = brain

    @property
    def brain(self) -> Brain:
        if self._brain is None:
            self._brain = build_brain(self.vertical_adapter.playbook())
        return self._brain

    # ------------------------------------------------------------------
    async def handle(
        self, session: AsyncSession, ctx: TenantContext, inbound: InboundMessage
    ) -> TurnResult:
        contact = await convo.resolve_contact(session, ctx, inbound)
        conversation = await convo.resolve_conversation(session, ctx, contact, inbound)

        # The clinic's own account answered manually (Telegram Business /
        # WhatsApp from the phone) → step back and let the human work.
        if inbound.from_business:
            await convo.store_inbound(session, ctx, conversation, inbound)
            conversation.assigned_to = "human"
            conversation.state = "with_human"
            await jobs.cancel(session, ctx.id, dedupe_prefix=f"followup:{conversation.id}")
            await session.flush()
            return TurnResult(handled=False, skipped_reason="business_side_message")

        stt_text = await self._maybe_transcribe(ctx, inbound)
        message = await convo.store_inbound(session, ctx, conversation, inbound, stt_text=stt_text)
        if message is None:
            return TurnResult(handled=False, skipped_reason="duplicate")

        text = (stt_text or inbound.text or "").strip()
        lang = ctx.lang_for(_detect_lang(text) or contact.lang)
        if lang != contact.lang:
            contact.lang = lang

        await log_event(
            session,
            ctx.id,
            INBOUND,
            contact_id=contact.id,
            conversation_id=conversation.id,
            data={"channel": inbound.channel_type, "type": inbound.type},
        )

        # A button tap is deterministic: never ask the model about it.
        if inbound.callback_data:
            handled = await self._handle_button(
                session, ctx, conversation, contact, inbound, lang
            )
            if handled is not None:
                return handled
            text = text or ""

        pre = precheck(text, lang=lang, extra_red_flags=ctx.settings.red_flags)

        if pre.has(Intent.OPT_OUT):
            contact.opted_out_at = now_utc()
            await jobs.cancel(session, ctx.id, dedupe_prefix=f"followup:{conversation.id}")
            await log_event(
                session, ctx.id, OPT_OUT, contact_id=contact.id, conversation_id=conversation.id
            )
            await self._say(session, ctx, conversation, contact, render(
                "opt_out_done", lang, overrides=ctx.settings.texts
            ))
            return TurnResult(handled=True, reply="opt_out")

        if pre.has(Intent.DELETE_DATA):
            await create_handoff(
                session, ctx, conversation,
                reason="other", priority="high",
                summary="Запрос на удаление персональных данных (ответить в 15 рабочих дней)",
            )
            await self._notify_handoff(session, ctx, conversation)
            await self._say(session, ctx, conversation, contact, render(
                "data_deleted", lang, overrides=ctx.settings.texts
            ))
            return TurnResult(handled=True, reply="delete_request", handoff_created=True)

        if pre.red_flag:
            await log_event(
                session, ctx.id, RED_FLAG, contact_id=contact.id,
                conversation_id=conversation.id, data={"matched": pre.matched},
            )
            await create_handoff(
                session, ctx, conversation, reason="red_flag", priority="urgent",
                summary=f"Красный флаг: {text[:180]}",
            )
            await self._notify_handoff(session, ctx, conversation)
            await self._say(session, ctx, conversation, contact, render(
                "red_flag", lang, overrides=ctx.settings.texts,
                phone=ctx.settings.phone or "",
                call_line=_emergency_line(ctx.settings.phone, lang),
            ))
            return TurnResult(handled=True, reply="red_flag", handoff_created=True)

        if inbound.type in ("image", "document", "video"):
            await create_handoff(
                session, ctx, conversation, reason="media", priority="normal",
                summary="Прислал фото/документ — нужен врач или администратор",
            )
            await self._notify_handoff(session, ctx, conversation)
            await self._say(session, ctx, conversation, contact, render(
                "photo_received", lang, overrides=ctx.settings.texts
            ))
            return TurnResult(handled=True, reply="media", handoff_created=True)

        if conversation.assigned_to != "bot":
            # A human owns this dialogue: store, ping them, stay quiet.
            await notify_staff(
                session, ctx, roles=["admin", "owner"],
                text=f"💬 Новое сообщение в диалоге с {contact.name or conversation.peer_id}:\n{text[:500]}",
            )
            return TurnResult(handled=False, skipped_reason="assigned_to_human")

        if not text:
            return TurnResult(handled=False, skipped_reason="empty_message")

        return await self._run_agent(session, ctx, conversation, contact, text, lang, pre)

    # ------------------------------------------------------------------
    async def _run_agent(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        contact: Contact,
        text: str,
        lang: str,
        pre,
    ) -> TurnResult:
        from .prompt import build_system_prompt

        vault = PIIVault()
        names = [n for n in [contact.name] if n]

        def mask(value: str) -> str:
            masked, _ = mask_text(
                value, vault, extra_names=names,
                enabled=get_settings().pii_masking_enabled,
            )
            return masked

        rows = await convo.history(session, conversation.id)
        messages = convo.to_llm_messages(rows, mask)
        if not messages:
            messages = [{"role": "user", "content": mask(text)}]

        tool_ctx = ToolContext(
            session=session,
            ctx=ctx,
            conversation=conversation,
            contact=contact,
            lang=lang,
            vault=vault,
            now=now_utc(),
        )
        state = self._state_for(conversation, contact, lang)
        system = build_system_prompt(
            ctx,
            lang=lang,
            vertical=self.vertical,
            now_local=to_tenant(now_utc(), ctx.tz_name).strftime("%A, %d.%m.%Y %H:%M"),
            extra_context=_state_lines(state, conversation),
        )
        if conversation.summary:
            system += f"\n\nКраткая история раньше: {mask(conversation.summary)}"

        used_tools: set[str] = set()

        async def execute(name: str, args: dict) -> dict:
            tool = self.registry.get(name)
            if tool is None:
                return {"error": "unknown_tool", "available": self.registry.names()}
            used_tools.add(name)
            out = await tool.handler(tool_ctx, args)
            await log_event(
                session, ctx.id, TOOL_CALL, contact_id=contact.id,
                conversation_id=conversation.id, entity_ref=name,
                data={"ok": not isinstance(out, dict) or "error" not in out},
            )
            return out

        result: BrainResult = await self.brain.run(
            system=system,
            messages=messages,
            tools=self.registry.schemas(),
            execute=execute,
            state=state,
        )

        reply = (result.text or "").strip()
        # The brain may route offers through a pseudo tool call.
        self._absorb_offers(conversation, result)

        violations: list[str] = []
        if reply:
            allow_prices = bool(used_tools & self.registry.price_tools())
            check = postcheck(
                reply, tool_names_used=used_tools, allow_prices=allow_prices,
                vertical=self.vertical,
            )
            if not check.ok:
                violations = check.violations
                log.warning(
                    "guardrail_blocked", violations=violations, tenant=str(ctx.id),
                    conversation=str(conversation.id),
                )
                await create_handoff(
                    session, ctx, conversation, reason="out_of_kb", priority="high",
                    summary=f"Ответ бота заблокирован проверкой: {', '.join(violations)}. "
                    f"Вопрос клиента: {text[:160]}",
                )
                await self._notify_handoff(session, ctx, conversation)
                reply = render("fallback", lang, overrides=ctx.settings.texts)

        handoff_created = False
        for intent in tool_ctx.intents:
            if intent.kind == "handoff":
                await create_handoff(
                    session, ctx, conversation,
                    reason=intent.payload.get("reason", "other"),
                    priority=intent.payload.get("priority", "normal"),
                    summary=intent.payload.get("summary") or text[:180],
                )
                await self._notify_handoff(session, ctx, conversation)
                handoff_created = True
            elif intent.kind in ("visit_booked", "trial_booked", "enrolled"):
                await self._on_conversion(session, ctx, conversation, intent)
            elif intent.kind in ("visit_cancelled", "trial_cancelled"):
                await self.vertical_adapter.on_conversion(
                    session, ctx, conversation, intent.kind, intent.payload
                )

        if not reply and handoff_created:
            reply = render("handoff_notice", lang, overrides=ctx.settings.texts)
        if not reply:
            reply = render("fallback", lang, overrides=ctx.settings.texts)

        reply = vault.restore(reply)[:MAX_REPLY_CHARS]
        buttons = self._buttons_for(conversation, tool_ctx)
        await self._say(session, ctx, conversation, contact, reply, buttons=buttons)

        await self._maybe_mark_interest(session, ctx, conversation, contact, used_tools, result)
        if not violations:
            await self._track_confusion(
                session, ctx, conversation, contact, used_tools, lang, result,
                handoff_created,
            )
        if result.usage:
            await log_event(
                session, ctx.id, "llm_usage", conversation_id=conversation.id,
                data={"model": result.model, **result.usage, "rounds": result.rounds},
            )

        return TurnResult(
            handled=True,
            reply=reply,
            tool_calls=sorted(used_tools),
            handoff_created=handoff_created,
            guard_violations=violations,
            usage=result.usage,
        )

    # ------------------------------------------------------------------
    def _state_for(self, conversation: Conversation, contact: Contact, lang: str) -> dict:
        scratch = conversation.scratch or {}
        return {
            "lang": lang,
            "vertical": self.vertical,
            "has_name": bool(contact.name),
            "has_phone": bool(contact.phone),
            "consent_pd": contact.consent_pd_at is not None,
            "pending_visit_id": scratch.get("pending_visit_id"),
            "still_needed": scratch.get("still_needed") or [],
            "offered_slots": scratch.get("offered_slots") or [],
            "reschedule_visit_id": scratch.get("reschedule_visit_id"),
            "confusion": scratch.get("confusion", 0),
        }

    def _absorb_offers(self, conversation: Conversation, result: BrainResult) -> None:
        """Remember what the agent offered so the next turn can resolve "10:00"."""
        scratch = dict(conversation.scratch or {})
        offered: list[dict] | None = None
        reschedule_id = scratch.get("reschedule_visit_id")
        pending = scratch.get("pending_visit_id")
        still_needed = scratch.get("still_needed")

        for name, _args, out in result.tool_calls:
            if not isinstance(out, dict):
                continue
            if name in ("find_slots", "__offer__") and out.get("slots"):
                offered = out["slots"]
                if out.get("reschedule_visit_id"):
                    reschedule_id = out["reschedule_visit_id"]
            if name == "hold_slot" and out.get("ok"):
                pending = out.get("visit_id")
                still_needed = out.get("still_needed") or []
                offered = []
            if name == "save_contact_fields" and out.get("saved"):
                known = out.get("known") or {}
                still_needed = [
                    field
                    for field in (still_needed or [])
                    if not known.get(field if field != "consent_pd" else "consent_pd")
                ]
            if name in ("confirm_booking", "reschedule") and out.get("ok"):
                pending = None
                still_needed = []
                offered = []
                reschedule_id = None
            if name == "cancel" and out.get("ok"):
                pending = None
                reschedule_id = None

        if offered is not None:
            scratch["offered_slots"] = offered[:6]
        scratch["pending_visit_id"] = pending
        scratch["still_needed"] = still_needed or []
        scratch["reschedule_visit_id"] = reschedule_id
        conversation.scratch = scratch

    def _buttons_for(self, conversation: Conversation, tool_ctx: ToolContext) -> list[Button]:
        for intent in tool_ctx.intents:
            if intent.kind == "quick_replies":
                return [Button(b["text"], b["data"]) for b in intent.payload.get("buttons", [])]
        offered = (conversation.scratch or {}).get("offered_slots") or []
        buttons: list[Button] = []
        for slot in offered[:3]:
            # Clinic offers carry slot_key; an education centre offers trial_key.
            # A missing key must not drop the whole reply.
            if slot.get("slot_key"):
                buttons.append(
                    Button(
                        f"{slot['time']} · {slot.get('doctor', '')}".strip(" ·"),
                        f"slot:{slot['slot_key']}",
                    )
                )
            elif slot.get("trial_key"):
                buttons.append(
                    Button(
                        str(slot.get("when") or "Пробное"),
                        f"trial:pick:{slot['trial_key']}",
                    )
                )
        return buttons

    async def _handle_button(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        contact: Contact,
        inbound: InboundMessage,
        lang: str,
    ) -> TurnResult | None:
        """Taps are deterministic: never ask the model what a button meant."""
        data = inbound.callback_data or ""
        if data == "optout":
            contact.opted_out_at = now_utc()
            await jobs.cancel(session, ctx.id, dedupe_prefix=f"followup:{conversation.id}")
            await self._say(
                session, ctx, conversation, contact,
                render("opt_out_done", lang, overrides=ctx.settings.texts),
            )
            return TurnResult(handled=True, reply="opt_out")

        outcome = await self.vertical_adapter.handle_button(
            session, ctx, conversation, contact, data, lang
        )
        if outcome is None:
            return None
        if outcome.reply:
            await self._say(session, ctx, conversation, contact, outcome.reply)
        if not outcome.handled:
            return None
        return TurnResult(handled=True, reply=outcome.label)

    async def _say(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        contact: Contact,
        text: str,
        *,
        buttons: list[Button] | None = None,
    ) -> None:
        first = conversation.last_outbound_at is None
        await send_message(
            session, ctx, conversation, text, buttons=buttons, contact=contact
        )
        if first and conversation.last_inbound_at:
            seconds = (now_utc() - conversation.last_inbound_at).total_seconds()
            await log_event(
                session, ctx.id, FIRST_RESPONSE, contact_id=contact.id,
                conversation_id=conversation.id, data={"seconds": round(seconds, 2)},
            )

    async def _notify_handoff(
        self, session: AsyncSession, ctx: TenantContext, conversation: Conversation
    ) -> None:
        from arix.domain.handoff import open_handoffs

        handoffs = await open_handoffs(session, ctx.id, limit=1)
        handoff = next((h for h in handoffs if h.conversation_id == conversation.id), None)
        if handoff is None or handoff.queued_until is not None or handoff.notified_at is not None:
            return
        text, buttons = await handoff_card(session, ctx, handoff)
        roles = ["admin", "owner"] if handoff.priority == "urgent" else ["admin"]
        sent = await notify_staff(session, ctx, roles=roles, text=text, buttons=buttons)
        if sent:
            handoff.notified_at = now_utc()
        elif not await staff_for_role(session, ctx.id, roles):
            log.warning("no_staff_configured", tenant=str(ctx.id))
        await session.flush()

    async def _on_conversion(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        intent,
    ) -> None:
        """A booking/enrolment happened: stop follow-ups, let the vertical act."""
        await jobs.cancel(session, ctx.id, dedupe_prefix=f"followup:{conversation.id}")
        await self.vertical_adapter.on_conversion(
            session, ctx, conversation, intent.kind, intent.payload
        )

    async def _maybe_mark_interest(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        contact: Contact,
        used_tools: set[str],
        result: BrainResult,
    ) -> None:
        """Asked about price/slots but did not book → arm the follow-up ladder."""
        if not ctx.settings.features.followup:
            return
        if contact.opted_out_at is not None:
            return
        interested = bool(used_tools & {"list_services", "find_slots", "route_by_complaint",
                                        "match_course", "list_groups"})
        booked = bool(used_tools & {"confirm_booking", "reschedule", "enroll", "book_trial"})
        if not interested or booked:
            return
        service_name = None
        for name, _args, out in result.tool_calls:
            if name == "list_services" and isinstance(out, dict) and out.get("services"):
                service_name = out["services"][0]["name"]
                break
        await log_event(
            session, ctx.id, INTEREST, contact_id=contact.id,
            conversation_id=conversation.id, data={"service": service_name},
        )
        offsets = ctx.settings.followup.offsets_h[: ctx.settings.followup.max_touches]
        for index, hours in enumerate(offsets, start=1):
            await jobs.enqueue(
                session, ctx.id, "followup",
                now_utc() + timedelta(hours=float(hours)),
                dedupe_key=jobs.dedupe("followup", conversation.id, index),
                payload={
                    "conversation_id": str(conversation.id),
                    "step": index,
                    "service": service_name,
                },
                timezone=ctx.tz_name,
                quiet=ctx.settings.quiet,
                replace=True,
            )

    async def _track_confusion(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        conversation: Conversation,
        contact: Contact,
        used_tools: set[str],
        lang: str,
        result: BrainResult,
        handoff_created: bool,
    ) -> None:
        """Two useless turns in a row → hand over, per ТЗ §6.3.

        A turn counts as useless only when nothing informative happened: no
        tool ran, or the only tool was a knowledge-base lookup that found
        nothing.  A KB hit *is* an answer and must not escalate.
        """
        scratch = dict(conversation.scratch or {})
        if handoff_created:
            confused = False
        elif not used_tools:
            confused = True
        elif used_tools <= {"kb_search", "get_contact_state"}:
            confused = not any(
                isinstance(out, dict) and out.get("facts")
                for name, _args, out in result.tool_calls
                if name == "kb_search"
            )
        else:
            confused = False
        count = int(scratch.get("confusion", 0))
        count = count + 1 if confused else 0
        scratch["confusion"] = count
        conversation.scratch = scratch
        if count >= ctx.settings.agent.max_confusion:
            scratch["confusion"] = 0
            conversation.scratch = scratch
            await create_handoff(
                session, ctx, conversation, reason="confusion", priority="normal",
                summary="Бот не смог ответить два сообщения подряд",
            )
            await self._notify_handoff(session, ctx, conversation)
            await self._say(
                session, ctx, conversation, contact,
                render("handoff_notice", lang, overrides=ctx.settings.texts),
            )

    async def _maybe_transcribe(self, ctx: TenantContext, inbound: InboundMessage) -> str | None:
        if inbound.type != "voice" or not ctx.settings.features.voice_stt:
            return None
        from arix.stt import transcribe

        return await transcribe(inbound)


def _emergency_line(phone: str | None, lang: str) -> str:
    if lang == "kz":
        return (
            f"{phone} нөміріне немесе 103-ке қоңырау шалыңыз." if phone
            else "103-ке қоңырау шалыңыз."
        )
    if lang == "uz":
        return f"{phone} yoki 103 ga qo'ng'iroq qiling." if phone else "103 ga qo'ng'iroq qiling."
    return (
        f"Позвоните нам {phone} или в скорую 103." if phone else "Позвоните в скорую 103."
    )


def _state_lines(state: dict, conversation: Conversation) -> list[str]:
    """Render dialogue state as prompt bullets so the model does not re-ask."""
    lines: list[str] = []
    known = [
        label
        for key, label in (
            ("has_name", "имя известно"),
            ("has_phone", "телефон известен"),
            ("consent_pd", "согласие на ПД получено"),
        )
        if state.get(key)
    ]
    lines.append("Известно о клиенте: " + (", ".join(known) if known else "ничего"))
    if state.get("pending_visit_id"):
        needed = state.get("still_needed") or []
        lines.append(
            f"Есть неподтверждённая бронь visit_id={state['pending_visit_id']}"
            + (f"; не хватает: {', '.join(needed)}" if needed else "; можно confirm_booking")
        )
    if state.get("reschedule_visit_id"):
        lines.append(
            f"Клиент переносит запись visit_id={state['reschedule_visit_id']} — "
            "используй reschedule, а не hold_slot"
        )
    offered = state.get("offered_slots") or []
    if offered:
        rendered = "; ".join(
            f"{slot.get('when') or slot.get('time')} → "
            f"{slot.get('slot_key') or slot.get('trial_key')}"
            for slot in offered
        )
        key_name = "slot_key" if any(slot.get("slot_key") for slot in offered) else "trial_key"
        lines.append(f"Уже предложенные слоты ({key_name} передавай как есть): {rendered}")
    if conversation.assigned_to != "bot":
        lines.append("Диалог ведёт человек — не отвечай сам.")
    return lines


def _detect_lang(text: str) -> str | None:
    """Cheap script/keyword language detection; the model refines from there."""
    if not text:
        return None
    lower = text.lower()
    kz_markers = ("қ", "ғ", "ң", "ө", "ұ", "ү", "һ", "і")
    if any(ch in lower for ch in kz_markers):
        return "kz"
    uz_markers = ("o'", "g'", "qancha", "qanday", "narxi", "salom", "rahmat", "bo'ladi")
    if any(marker in lower for marker in uz_markers):
        return "uz"
    if any("а" <= ch <= "я" or ch == "ё" for ch in lower):
        return "ru"
    return None


__all__ = ["ButtonOutcome", "ConversationRuntime", "TurnResult", "VerticalAdapter"]
