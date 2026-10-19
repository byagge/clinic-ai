"""Deterministic brain used when no LLM key is configured.

It is not a toy: it drives the same tools, respects the same guardrails and
covers the acceptance scenarios of ТЗ §7 (price question, booking, complaint,
reschedule, cancel, "my visits").  That makes three things possible:

* ``docker compose up`` demos the product with no API key and no network;
* the eval suite has a deterministic baseline to diff against;
* if the Anthropic API is down, the clinic keeps booking patients.

It speaks deliberately plainly — short, factual, no small talk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .brain import BrainResult, ToolExecutor
from .guardrails import Intent, precheck

TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.\s]?([0-5]\d)\b")
#: The brain sees PII-masked history, so a phone arrives as ``[PHONE_1]``.
#: Tokens are passed through to the tools, which restore them from the vault.
PII_PHONE_TOKEN = re.compile(r"\[PHONE_\d+\]")
PII_NAME_TOKEN = re.compile(r"\[NAME_\d+\]")
ORDINAL_WORDS = {
    "перв": 0, "втор": 1, "трет": 2, "первый": 0, "второй": 1, "третий": 2,
    "1": 0, "2": 1, "3": 2, "один": 0, "два": 1, "три": 2,
}
YES = {"да", "ок", "окей", "хорошо", "давай", "давайте", "согласен", "согласна", "подтверждаю",
       "yes", "ok", "жарайды", "иә", "ha", "xo'p", "подходит", "беру", "записывайте", "+"}
NO = {"нет", "не надо", "не нужно", "потом", "позже", "no", "жоқ", "yo'q"}
BOOK_WORDS = ("запиш", "записаться", "хочу запис", "можно запис", "забронир", "navbat", "жазыл")
RESCHEDULE_WORDS = ("перенес", "перенести", "другое время", "поменять время", "ko'chir", "ауыстыр")
CANCEL_WORDS = ("отмен", "не приду", "откаж", "bekor", "болдырмау")
MY_VISITS_WORDS = ("мои записи", "когда я записан", "моя запись", "когда приём", "когда прием")
#: "можно ли отменить бесплатно?" asks about the rules, it does not cancel.
POLICY_WORDS = ("можно ли", "правила", "условия", "бесплатно", "за сколько", "сколько часов",
                "какие условия", "политика")
#: Questions *about* a service rather than its price: answer from the base
#: first, otherwise "как готовиться к чистке?" gets a price list back.
KB_FIRST_WORDS = ("как готовит", "подготов", "что взять", "что нужно взять", "больно ли",
                  "как проходит", "сколько длится", "противопоказан", "можно ли",
                  "после процедуры", "что нельзя", "как часто", "с какого возраста",
                  "нужны ли документ", "где вы", "адрес", "как добраться", "парковк",
                  "часы работы", "во сколько вы", "работаете ли")
SLOT_WORDS = ("когда", "свободн", "окно", "время", "можно прийти", "запис")


@dataclass
class _Reply:
    text: str


class ScriptedBrain:
    """Rule-based dialogue manager over the clinic tool set."""

    model = "scripted"
    vertical = "clinic"

    async def run(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        execute: ToolExecutor,
        state: dict | None = None,
    ) -> BrainResult:
        state = state or {}
        result = BrainResult(text="", model=self.model, rounds=1)
        user_text = _last_user_text(messages)
        lower = user_text.lower().strip()
        lang = state.get("lang", "ru")
        pre = precheck(user_text, lang=lang)

        async def call(name: str, args: dict) -> dict:
            out = await execute(name, args)
            result.tool_calls.append((name, args, out))
            return out

        # --- hard routes ------------------------------------------------
        if pre.has(Intent.RED_FLAG):
            await call("handoff", {"reason": "red_flag", "priority": "urgent",
                                   "summary": "Красный флаг в сообщении пациента"})
            result.text = ""  # the runtime sends the red-flag template verbatim
            return result

        if pre.has(Intent.HUMAN_REQUEST):
            await call("handoff", {"reason": "patient_request", "priority": "normal",
                                   "summary": "Просит человека"})
            result.text = "Подключаю администратора — он ответит здесь."
            return result

        if pre.has(Intent.DISCOUNT) or pre.has(Intent.REFUND):
            await call("handoff", {"reason": "discount" if pre.has(Intent.DISCOUNT) else "refund",
                                   "priority": "normal", "summary": "Торг / возврат"})
            result.text = "Такие вопросы решает администратор — передаю ему, ответит здесь."
            return result

        if pre.has(Intent.DIAGNOSIS):
            await call("handoff", {"reason": "out_of_kb", "priority": "normal",
                                   "summary": "Просит диагноз или назначение"})
            result.text = (
                "Диагноз и лечение назначает только врач на приёме — я не могу этого сделать. "
                "Передаю администратору, он подберёт время."
            )
            return result

        # --- pending booking flow --------------------------------------
        pending = state.get("pending_visit_id")
        if pending:
            needed = list(state.get("still_needed") or [])
            if needed:
                reply = await self._collect_booking_fields(call, pending, needed, user_text, lower)
                if reply:
                    result.text = reply.text
                    return result
            confirmed = await call("confirm_booking", {"visit_id": pending})
            if confirmed.get("ok"):
                result.text = confirmed["confirmation_text"]
            elif confirmed.get("error") == "missing_fields":
                result.text = _ask_for(confirmed.get("missing", []))
            elif confirmed.get("error") == "hold_expired":
                result.text = "Бронь истекла. Давайте подберём время заново — на какой день удобно?"
            else:
                await call("handoff", {"reason": "other", "priority": "normal",
                                       "summary": "Не удалось подтвердить запись"})
                result.text = "Передаю администратору, он подтвердит запись."
            return result

        # --- slot choice from a previous offer -------------------------
        offered = state.get("offered_slots") or []
        if offered:
            chosen = _match_offered(lower, offered)
            if chosen is not None and state.get("reschedule_visit_id"):
                moved = await call(
                    "reschedule",
                    {"visit_id": state["reschedule_visit_id"], "slot_key": chosen["slot_key"]},
                )
                if moved.get("ok"):
                    result.text = moved.get("confirmation_text") or "Перенесла запись."
                elif moved.get("error") == "slot_taken":
                    result.text = "Это время только что заняли. Подобрать другое?"
                else:
                    await call("handoff", {"reason": "other", "priority": "normal",
                                           "summary": "Не удалось перенести запись"})
                    result.text = "Передаю администратору — он перенесёт запись."
                return result
            if chosen is not None:
                held = await call("hold_slot", {"slot_key": chosen["slot_key"]})
                if held.get("ok"):
                    needs = held.get("still_needed") or []
                    if needs:
                        result.text = _ask_for(needs)
                    else:
                        done = await call("confirm_booking", {"visit_id": held["visit_id"]})
                        result.text = done.get("confirmation_text") or "Записала вас."
                elif held.get("error") == "slot_taken":
                    result.text = "Это время только что заняли. Подобрать другое?"
                else:
                    result.text = "Не получилось придержать это время. Назовите другое, пожалуйста."
                return result
            if lower in NO:
                result.text = "Хорошо. Если передумаете — напишите, подберу время."
                return result

        # --- explicit flows -------------------------------------------
        if _any(lower, MY_VISITS_WORDS):
            visits = await call("my_visits", {})
            if visits.get("count"):
                lines = [f"{v['when']} — {v['service']}, {v['doctor']}" for v in visits["visits"]]
                result.text = "Ваши записи:\n" + "\n".join(lines)
            else:
                result.text = "Активных записей нет. Подобрать время?"
            return result

        if _any(lower, CANCEL_WORDS) and not _any(lower, POLICY_WORDS):
            visits = await call("my_visits", {})
            if visits.get("count"):
                out = await call("cancel", {"visit_id": visits["visits"][0]["visit_id"],
                                            "reason": "patient"})
                result.text = out.get("text") or "Отменила запись."
            else:
                result.text = "Не нашла активной записи. Подсказать свободное время?"
            return result

        if _any(lower, RESCHEDULE_WORDS) and not _any(lower, POLICY_WORDS):
            visits = await call("my_visits", {})
            if not visits.get("count"):
                result.text = "Активной записи не нашла. Подобрать новое время?"
                return result
            visit = visits["visits"][0]
            service = await self._guess_service(call, visit["service"])
            slots = await call("find_slots", {"service_id": service, "count": 3}) if service else {}
            if slots.get("count"):
                state_slots = slots["slots"]
                result.text = (
                    f"Сейчас у вас {visit['when']}. Свободно: "
                    + ", ".join(s["when"] for s in state_slots)
                    + ". На какое перенести?"
                )
                result.tool_calls.append(("__offer__", {}, {"slots": state_slots,
                                                            "reschedule_visit_id": visit["visit_id"]}))
            else:
                await call("handoff", {"reason": "other", "priority": "normal",
                                       "summary": "Перенос, свободных слотов нет"})
                result.text = "Свободных окон не вижу — передаю администратору, он подберёт время."
            return result

        # --- complaint routing (never a diagnosis) -----------------------
        if len(lower) > 12 and _any(lower, ("болит", "беспокоит", "мешает", "ауырады", "og'riyapti")):
            routed = await call("route_by_complaint", {"complaint_text": user_text})
            service = (routed.get("services") or [None])[0]
            if service:
                slots = await call("find_slots", {"service_id": service["service_id"], "count": 2})
                if slots.get("slots"):
                    result.tool_calls.append(("__offer__", {}, {"slots": slots["slots"]}))
                when = ", ".join(slot["when"] for slot in slots.get("slots", []))
                result.text = (
                    f"Это к «{service['name']}» — {service['price']}. "
                    + (f"Есть {when}. Записать?" if when else "Подобрать время?")
                )
                return result

        # --- the service / knowledge-base cascade -------------------------
        # A clinic message is most often about a service, so try the price
        # list first, then the knowledge base, and only then a human. Nothing
        # in this chain can state a price that did not come from the database.
        # Questions about *how* a service works go to the base first.
        if _any(lower, KB_FIRST_WORDS) and not pre.has(Intent.PRICE):
            kb = await call("kb_search", {"query": user_text, "lang": lang})
            facts = kb.get("facts") or []
            if facts:
                result.text = facts[0]["answer"]
                return result

        reply = await self._service_flow(call, result, user_text, lang)
        if reply is not None:
            result.text = reply.text
            return result

        if lower in YES:
            result.text = "Подскажите, на какую услугу вас записать?"
            return result

        await call("handoff", {"reason": "confusion", "priority": "normal",
                               "summary": f"Не разобрала запрос: {user_text[:100]}"})
        result.text = "Не до конца поняла вопрос. Передаю администратору — он ответит здесь."
        return result

    # ------------------------------------------------------------------
    async def _service_flow(self, call, result, user_text: str, lang: str) -> _Reply | None:
        """Price list → knowledge base → human, in that order.

        Returns ``None`` only when none of the three produced anything, which
        the caller turns into the generic fallback.
        """
        services = await call("list_services", {"query": user_text})
        items = services.get("services") or []
        if not items or not services.get("query_matched", True):
            # The query matched nothing in the price list.  Quoting the first
            # service here would be exactly the invented price ТЗ §0 forbids.
            kb = await call("kb_search", {"query": user_text, "lang": lang})
            facts = kb.get("facts") or []
            if facts:
                return _Reply(facts[0]["answer"])
            await call("handoff", {"reason": "out_of_kb", "priority": "normal",
                                   "summary": f"Нет ответа в базе по запросу: {user_text[:80]}"})
            return _Reply(
                "Такого в базе нет — уточню у администратора и вернусь с ответом."
            )

        top = items[0]
        line = f"{top['name']} — {top['price']}, {top['duration_min']} мин."
        if not top["online_bookable"]:
            await call("handoff", {"reason": "not_online_bookable", "priority": "normal",
                                   "summary": f"Запись на {top['name']} только через админа"})
            return _Reply(f"{line} На неё записывает администратор — передаю ему.")

        slots = await call("find_slots", {"service_id": top["service_id"], "count": 3})
        if not slots.get("count"):
            return _Reply(
                f"{line} Свободных окон в ближайшие дни нет — подобрать на следующую неделю?"
            )
        result.tool_calls.append(("__offer__", {}, {"slots": slots["slots"]}))
        when = ", ".join(slot["when"] for slot in slots["slots"])
        return _Reply(f"{line} Записать? Есть {when}.")

    async def _collect_booking_fields(
        self, call, visit_id: str, needed: list[str], text: str, lower: str
    ) -> _Reply | None:
        payload: dict = {}
        phone_given = _is_phone(text)
        if "phone" in needed and phone_given:
            payload["phone"] = text.strip()
        if "consent_pd" in needed and (lower in YES or "соглас" in lower):
            payload["consent_pd"] = True
        if "name" in needed and not phone_given:
            candidate = text.strip().strip(".,!")
            if 2 <= len(candidate) <= 60 and _is_name(candidate):
                payload["name"] = candidate
        if payload:
            await call("save_contact_fields", payload)
            remaining = [f for f in needed if f not in payload]
            if remaining:
                return _Reply(_ask_for(remaining))
            return None
        return _Reply(_ask_for(needed))

    async def _guess_service(self, call, service_name: str) -> str | None:
        services = await call("list_services", {"query": service_name})
        items = services.get("services") or []
        return items[0]["service_id"] if items else None


def _ask_for(fields: list[str]) -> str:
    if "name" in fields:
        return "Как вас записать — скажите, пожалуйста, имя?"
    if "phone" in fields:
        return "Оставьте номер телефона для записи."
    if "consent_pd" in fields:
        return (
            "Нужно согласие на обработку персональных данных — имя и телефон только для записи. "
            "Подтверждаете?"
        )
    return "Уточните, пожалуйста, детали записи."


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                return "\n".join(texts)
    return ""


def _any(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def _is_phone(text: str) -> bool:
    from arix.core.pii import looks_like_phone

    return bool(PII_PHONE_TOKEN.search(text or "")) or looks_like_phone(text or "")


def _is_name(text: str) -> bool:
    if PII_NAME_TOKEN.search(text or ""):
        return True
    stripped = PII_PHONE_TOKEN.sub("", text or "")
    return bool(stripped.strip()) and not any(ch.isdigit() for ch in stripped)


def _match_offered(lower: str, offered: list[dict]) -> dict | None:
    """Resolve "10:00", "второй", "завтра" or an exact echo to an offered slot."""
    match = TIME_RE.search(lower)
    if match:
        wanted = f"{int(match.group(1)):02d}:{match.group(2)}"
        for slot in offered:
            if slot.get("time") == wanted:
                return slot
    for token, index in ORDINAL_WORDS.items():
        if token in lower and index < len(offered):
            return offered[index]
    for slot in offered:
        when = (slot.get("when") or "").lower()
        if when and when in lower:
            return slot
    if lower.strip() in YES and offered:
        return offered[0]
    return None


__all__ = ["ScriptedBrain"]
