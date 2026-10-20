"""Deterministic brain used when no LLM key is configured.

It is not a stub: it drives the same tools, respects the same guardrails and
passes the same eval suite as Claude.  That buys three things:

* ``docker compose up`` demos the product with no API key and no network;
* the eval suite has a deterministic baseline to diff prompt changes against;
* if the Anthropic API is down, the centre keeps booking and billing.

This module owns only what every vertical shares: the hard routes that must
never depend on a model (emergency, "give me a human", bargaining, refunds)
and the small helpers for collecting a name, a phone and consent.  Everything
domain-specific lives in a :class:`Playbook` supplied by the vertical, which
is what keeps ``arix/agent`` identical across repositories.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .brain import BrainResult, ToolExecutor
from .guardrails import Intent, PreCheck, precheck

TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.\s]?([0-5]\d)\b")
#: The brain sees PII-masked history, so a phone arrives as ``[PHONE_1]``.
#: Tokens are passed through to the tools, which restore them from the vault.
PII_PHONE_TOKEN = re.compile(r"\[PHONE_\d+\]")
PII_NAME_TOKEN = re.compile(r"\[NAME_\d+\]")

ORDINAL_WORDS = {
    "перв": 0, "втор": 1, "трет": 2, "1": 0, "2": 1, "3": 2,
    "один": 0, "два": 1, "три": 2, "первый": 0, "второй": 1, "третий": 2,
}
YES = {
    "да", "ок", "окей", "хорошо", "давай", "давайте", "согласен", "согласна",
    "подтверждаю", "yes", "ok", "жарайды", "иә", "ha", "xo'p", "подходит",
    "беру", "записывайте", "+", "ага", "конечно",
}
NO = {"нет", "не надо", "не нужно", "потом", "позже", "no", "жоқ", "yo'q"}


@dataclass
class Reply:
    """What the playbook decided to say."""

    text: str


@dataclass
class Turn:
    """Everything a playbook needs for one turn."""

    text: str
    lower: str
    lang: str
    state: dict
    pre: PreCheck
    call: Callable[[str, dict], Awaitable[dict]]
    result: BrainResult

    def offer(self, items: list[dict], **extra) -> None:
        """Remember what was offered so the next turn can resolve "10:00"."""
        self.result.tool_calls.append(("__offer__", {}, {"slots": items, **extra}))


class Playbook(Protocol):
    """Vertical-specific dialogue flow."""

    async def respond(self, turn: Turn) -> Reply | None: ...


class ScriptedBrain:
    """Rule-based dialogue manager: shared safety routes plus a playbook."""

    model = "scripted"

    def __init__(self, playbook: Playbook) -> None:
        self.playbook = playbook

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
        user_text = last_user_text(messages)
        lower = user_text.lower().strip()
        lang = state.get("lang", "ru")
        pre = precheck(user_text, lang=lang)

        async def call(name: str, args: dict) -> dict:
            out = await execute(name, args)
            result.tool_calls.append((name, args, out))
            return out

        # --- hard routes, identical in every vertical -------------------
        if pre.has(Intent.RED_FLAG):
            await call(
                "handoff",
                {"reason": "red_flag", "priority": "urgent",
                 "summary": "Красный флаг в сообщении клиента"},
            )
            # The runtime sends the red-flag template verbatim.
            return result

        if pre.has(Intent.HUMAN_REQUEST):
            await call(
                "handoff",
                {"reason": "patient_request", "priority": "normal", "summary": "Просит человека"},
            )
            result.text = "Подключаю администратора — он ответит здесь."
            return result

        if pre.has(Intent.DISCOUNT) or pre.has(Intent.REFUND):
            reason = "discount" if pre.has(Intent.DISCOUNT) else "refund"
            await call(
                "handoff",
                {"reason": reason, "priority": "normal", "summary": "Торг или возврат"},
            )
            result.text = "Такие вопросы решает администратор — передаю ему, ответит здесь."
            return result

        # --- vertical flow ---------------------------------------------
        turn = Turn(
            text=user_text, lower=lower, lang=lang, state=state, pre=pre,
            call=call, result=result,
        )
        reply = await self.playbook.respond(turn)
        if reply is not None:
            result.text = reply.text
            return result

        await call(
            "handoff",
            {"reason": "confusion", "priority": "normal",
             "summary": f"Не разобрала запрос: {user_text[:100]}"},
        )
        result.text = "Не до конца поняла вопрос. Передаю администратору — он ответит здесь."
        return result


# ---------------------------------------------------------------------------
# Helpers shared by the playbooks
# ---------------------------------------------------------------------------


def ask_for(fields: list[str]) -> str:
    """One question at a time, in the order that converts best."""
    if "name" in fields:
        return "Как вас записать — скажите, пожалуйста, имя?"
    if "phone" in fields:
        return "Оставьте номер телефона для записи."
    if "consent_pd" in fields:
        return (
            "Нужно согласие на обработку персональных данных — имя и телефон только для "
            "записи. Подтверждаете?"
        )
    return "Уточните, пожалуйста, детали записи."


async def collect_contact_fields(
    turn: Turn, needed: list[str], *, name_label: str = "name"
) -> Reply | None:
    """Save whatever the message contains; ask for what is still missing.

    Returns ``None`` once nothing is missing, so the caller can proceed.
    """
    payload: dict = {}
    phone_given = is_phone(turn.text)
    if "phone" in needed and phone_given:
        payload["phone"] = turn.text.strip()
    if "consent_pd" in needed and (turn.lower in YES or "соглас" in turn.lower):
        payload["consent_pd"] = True
    if name_label in needed and not phone_given:
        candidate = turn.text.strip().strip(".,!")
        if 2 <= len(candidate) <= 60 and is_name(candidate):
            payload["name"] = candidate

    if payload:
        await turn.call("save_contact_fields", payload)
        remaining = [field for field in needed if field not in payload]
        return Reply(ask_for(remaining)) if remaining else None
    return Reply(ask_for(needed))


def is_phone(text: str) -> bool:
    from arix.core.pii import looks_like_phone

    return bool(PII_PHONE_TOKEN.search(text or "")) or looks_like_phone(text or "")


def is_name(text: str) -> bool:
    if PII_NAME_TOKEN.search(text or ""):
        return True
    stripped = PII_PHONE_TOKEN.sub("", text or "")
    return bool(stripped.strip()) and not any(ch.isdigit() for ch in stripped)


def last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
    return ""


def has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def match_offered(lower: str, offered: list[dict]) -> dict | None:
    """Resolve "10:00", "второй", an exact echo or a bare "да" to an offer."""
    match = TIME_RE.search(lower)
    if match:
        wanted = f"{int(match.group(1)):02d}:{match.group(2)}"
        for item in offered:
            if item.get("time") == wanted:
                return item
    for token, index in ORDINAL_WORDS.items():
        if token in lower and index < len(offered):
            return offered[index]
    for item in offered:
        when = (item.get("when") or "").lower()
        if when and when in lower:
            return item
    if lower.strip() in YES and offered:
        return offered[0]
    return None


__all__ = [
    "NO",
    "YES",
    "Playbook",
    "Reply",
    "ScriptedBrain",
    "Turn",
    "ask_for",
    "collect_contact_fields",
    "has_any",
    "is_name",
    "is_phone",
    "last_user_text",
    "match_offered",
]
