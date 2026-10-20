"""Deterministic dialogue flow for a clinic.

Covers the acceptance scenarios of ТЗ §7: price, booking, complaint, reschedule,
cancel and "my visits".  It speaks plainly and never states a price that did
not come from a tool.
"""
from __future__ import annotations

from arix.agent.guardrails import Intent
from arix.agent.scripted import (
    NO,
    Reply,
    Turn,
    ask_for,
    collect_contact_fields,
    has_any,
    match_offered,
)

RESCHEDULE_WORDS = ("перенес", "перенести", "другое время", "поменять время", "ko'chir", "ауыстыр")
CANCEL_WORDS = ("отмен", "не приду", "откаж", "bekor", "болдырмау")
MY_VISITS_WORDS = ("мои записи", "когда я записан", "моя запись", "когда приём", "когда прием")
#: "можно ли отменить бесплатно?" asks about the rules, it does not cancel.
POLICY_WORDS = (
    "можно ли", "правила", "условия", "бесплатно", "за сколько", "сколько часов",
    "какие условия", "политика",
)
#: Questions *about* a service rather than its price go to the base first,
#: otherwise "как готовиться к чистке?" gets a price list back.
KB_FIRST_WORDS = (
    "как готовит", "подготов", "что взять", "что нужно взять", "больно ли",
    "как проходит", "сколько длится", "противопоказан", "можно ли",
    "после процедуры", "что нельзя", "как часто", "с какого возраста",
    "нужны ли документ", "где вы", "адрес", "как добраться", "парковк",
    "часы работы", "во сколько вы", "работаете ли",
)
COMPLAINT_WORDS = ("болит", "беспокоит", "мешает", "ауырады", "og'riyapti")


class ClinicPlaybook:
    """Vertical flow handed to :class:`arix.agent.scripted.ScriptedBrain`."""

    async def respond(self, turn: Turn) -> Reply | None:
        state = turn.state
        lower = turn.lower

        if turn.pre.has(Intent.DIAGNOSIS):
            await turn.call(
                "handoff",
                {"reason": "out_of_kb", "priority": "normal",
                 "summary": "Просит диагноз или назначение"},
            )
            return Reply(
                "Диагноз и лечение назначает только врач на приёме — я не могу этого сделать. "
                "Передаю администратору, он подберёт время."
            )

        pending = state.get("pending_visit_id")
        if pending:
            return await self._finish_pending(turn, pending)

        offered = state.get("offered_slots") or []
        if offered:
            chosen = match_offered(lower, offered)
            if chosen is not None and state.get("reschedule_visit_id"):
                return await self._reschedule_to(turn, state["reschedule_visit_id"], chosen)
            if chosen is not None:
                return await self._hold_offered(turn, chosen)
            if lower in NO:
                return Reply("Хорошо. Если передумаете — напишите, подберу время.")

        if has_any(lower, MY_VISITS_WORDS):
            return await self._my_visits(turn)

        if has_any(lower, CANCEL_WORDS) and not has_any(lower, POLICY_WORDS):
            return await self._cancel(turn)

        if has_any(lower, RESCHEDULE_WORDS) and not has_any(lower, POLICY_WORDS):
            return await self._start_reschedule(turn)

        if len(lower) > 12 and has_any(lower, COMPLAINT_WORDS):
            routed = await self._route_complaint(turn)
            if routed is not None:
                return routed

        if has_any(lower, KB_FIRST_WORDS) and not turn.pre.has(Intent.PRICE):
            kb = await turn.call("kb_search", {"query": turn.text, "lang": turn.lang})
            facts = kb.get("facts") or []
            if facts:
                return Reply(facts[0]["answer"])

        reply = await self._service_flow(turn)
        if reply is not None:
            return reply

        if lower in {"да", "ок", "окей", "хорошо", "давай", "давайте"}:
            return Reply("Подскажите, на какую услугу вас записать?")
        return None

    async def _finish_pending(self, turn: Turn, visit_id: str) -> Reply:
        needed = list(turn.state.get("still_needed") or [])
        if needed:
            collected = await collect_contact_fields(turn, needed)
            if collected is not None:
                return collected
        confirmed = await turn.call("confirm_booking", {"visit_id": visit_id})
        if confirmed.get("ok"):
            return Reply(confirmed["confirmation_text"])
        if confirmed.get("error") == "missing_fields":
            return Reply(ask_for(confirmed.get("missing", [])))
        if confirmed.get("error") == "hold_expired":
            return Reply("Бронь истекла. Давайте подберём время заново — на какой день удобно?")
        await turn.call(
            "handoff",
            {"reason": "other", "priority": "normal", "summary": "Не удалось подтвердить запись"},
        )
        return Reply("Передаю администратору, он подтвердит запись.")

    async def _reschedule_to(self, turn: Turn, visit_id: str, chosen: dict) -> Reply:
        moved = await turn.call(
            "reschedule",
            {"visit_id": visit_id, "slot_key": chosen["slot_key"]},
        )
        if moved.get("ok"):
            return Reply(moved.get("confirmation_text") or "Перенесла запись.")
        if moved.get("error") == "slot_taken":
            return Reply("Это время только что заняли. Подобрать другое?")
        await turn.call(
            "handoff",
            {"reason": "other", "priority": "normal", "summary": "Не удалось перенести запись"},
        )
        return Reply("Передаю администратору — он перенесёт запись.")

    async def _hold_offered(self, turn: Turn, chosen: dict) -> Reply:
        held = await turn.call("hold_slot", {"slot_key": chosen["slot_key"]})
        if held.get("ok"):
            needs = held.get("still_needed") or []
            if needs:
                return Reply(ask_for(needs))
            done = await turn.call("confirm_booking", {"visit_id": held["visit_id"]})
            return Reply(done.get("confirmation_text") or "Записала вас.")
        if held.get("error") == "slot_taken":
            return Reply("Это время только что заняли. Подобрать другое?")
        return Reply("Не получилось придержать это время. Назовите другое, пожалуйста.")

    async def _my_visits(self, turn: Turn) -> Reply:
        visits = await turn.call("my_visits", {})
        if visits.get("count"):
            lines = [f"{item['when']} — {item['service']}, {item['doctor']}" for item in visits["visits"]]
            return Reply("Ваши записи:\n" + "\n".join(lines))
        return Reply("Активных записей нет. Подобрать время?")

    async def _cancel(self, turn: Turn) -> Reply:
        visits = await turn.call("my_visits", {})
        if visits.get("count"):
            out = await turn.call(
                "cancel",
                {"visit_id": visits["visits"][0]["visit_id"], "reason": "patient"},
            )
            return Reply(out.get("text") or "Отменила запись.")
        return Reply("Не нашла активной записи. Подсказать свободное время?")

    async def _start_reschedule(self, turn: Turn) -> Reply:
        visits = await turn.call("my_visits", {})
        if not visits.get("count"):
            return Reply("Активной записи не нашла. Подобрать новое время?")
        visit = visits["visits"][0]
        service = await self._guess_service(turn, visit["service"])
        slots = (
            await turn.call("find_slots", {"service_id": service, "count": 3}) if service else {}
        )
        if slots.get("count"):
            offered = slots["slots"]
            turn.offer(offered, reschedule_visit_id=visit["visit_id"])
            when = ", ".join(slot["when"] for slot in offered)
            return Reply(f"Сейчас у вас {visit['when']}. Свободно: {when}. На какое перенести?")
        await turn.call(
            "handoff",
            {"reason": "other", "priority": "normal", "summary": "Перенос, свободных слотов нет"},
        )
        return Reply("Свободных окон не вижу — передаю администратору, он подберёт время.")

    async def _route_complaint(self, turn: Turn) -> Reply | None:
        routed = await turn.call("route_by_complaint", {"complaint_text": turn.text})
        service = (routed.get("services") or [None])[0]
        if not service:
            return None
        slots = await turn.call("find_slots", {"service_id": service["service_id"], "count": 2})
        if slots.get("slots"):
            turn.offer(slots["slots"])
        when = ", ".join(slot["when"] for slot in slots.get("slots", []))
        return Reply(
            f"Это к «{service['name']}» — {service['price']}. "
            + (f"Есть {when}. Записать?" if when else "Подобрать время?")
        )

    async def _service_flow(self, turn: Turn) -> Reply | None:
        """Price list → knowledge base → human. Never invents a price."""
        services = await turn.call("list_services", {"query": turn.text})
        items = services.get("services") or []
        if not items or not services.get("query_matched", True):
            kb = await turn.call("kb_search", {"query": turn.text, "lang": turn.lang})
            facts = kb.get("facts") or []
            if facts:
                return Reply(facts[0]["answer"])
            await turn.call(
                "handoff",
                {"reason": "out_of_kb", "priority": "normal",
                 "summary": f"Нет ответа в базе по запросу: {turn.text[:80]}"},
            )
            return Reply("Такого в базе нет — уточню у администратора и вернусь с ответом.")

        top = items[0]
        line = f"{top['name']} — {top['price']}, {top['duration_min']} мин."
        if not top["online_bookable"]:
            await turn.call(
                "handoff",
                {"reason": "not_online_bookable", "priority": "normal",
                 "summary": f"Запись на {top['name']} только через админа"},
            )
            return Reply(f"{line} На неё записывает администратор — передаю ему.")

        slots = await turn.call("find_slots", {"service_id": top["service_id"], "count": 3})
        if not slots.get("count"):
            return Reply(f"{line} Свободных окон в ближайшие дни нет — подобрать на следующую неделю?")
        turn.offer(slots["slots"])
        when = ", ".join(slot["when"] for slot in slots["slots"])
        return Reply(f"{line} Записать? Есть {when}.")

    async def _guess_service(self, turn: Turn, service_name: str) -> str | None:
        services = await turn.call("list_services", {"query": service_name})
        items = services.get("services") or []
        return items[0]["service_id"] if items else None


__all__ = ["ClinicPlaybook"]
