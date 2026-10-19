"""Guardrails around the model.

Three independent layers, because a prompt alone is not a safety control:

* **pre** — classify the inbound message (red flag, opt-out, human request,
  data-deletion request, prompt injection) before any model call;
* **tools** — the only source of prices, doctors, slots and policy text;
* **post** — validate the drafted reply: a message that states a price or a
  diagnosis without a backing tool result is replaced by a handoff.

Everything here is pure and unit-tested.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Lexicons. Kept explicit (not learned) so a clinic can audit them, and
# extendable per tenant through ``tenant_config.red_flags``.
# ---------------------------------------------------------------------------

RED_FLAGS_DEFAULT: dict[str, list[str]] = {
    "ru": [
        "сильная боль", "очень болит", "невыносимо болит", "опухла", "опухло", "отек",
        "отёк", "флюс", "кровотечение", "кровь не останавливается", "температура",
        "39", "38.5", "гной", "нагноение", "травма", "выбил зуб", "сломал зуб",
        "задыхаюсь", "трудно дышать", "тяжело дышать", "онемение", "аллергия",
        "анафилакс", "потерял сознание", "обморок",
    ],
    "kz": [
        "қатты ауырады", "ісіп кетті", "ісік", "қан кетіп", "қызба", "температура",
        "жарақат", "тыныс алу қиын", "ірің",
    ],
    "uz": [
        "qattiq og'riyapti", "shishib ketdi", "shish", "qon ketyapti", "isitma",
        "jarohat", "nafas olish qiyin", "yiring",
    ],
}

HUMAN_REQUEST = [
    "человек", "оператор", "администратор", "менеджер", "живой", "позвоните",
    "позвонить", "call me", "адам", "operator", "odam", "real person",
]

OPT_OUT = [
    "стоп", "stop", "отпишите", "отписаться", "не пишите", "не надо писать",
    "unsubscribe", "больше не пишите", "тоқтат", "to'xtat", "yozmang",
]

DELETE_REQUEST = [
    "удалите мои данные", "удалить мои данные", "удалите данные",
    "delete my data", "деректерімді жойыңыз", "ma'lumotlarimni o'chiring",
]

DISCOUNT = [
    "скидк", "дешевле", "подешевле", "торг", "уступите", "дорого",
    "акция", "промокод", "арзан", "chegirma", "arzon",
]

DIAGNOSIS_REQUEST = [
    "диагноз", "что у меня", "это кариес", "это пульпит", "это рак",
    "что это может быть", "какое лечение", "какие таблетки", "какое лекарство",
    "что выпить", "что принять", "чем лечить", "что мне принимать",
]

REFUND = ["возврат", "верните деньги", "вернуть деньги", "refund", "ақшаны қайтар", "pulni qaytar"]

#: Phrases that try to override the system prompt. We do not attempt to detect
#: every jailbreak — we simply never let model output bypass the post-checks.
INJECTION = [
    "забудь инструкции", "забудь все инструкции", "ignore previous",
    "ignore all previous", "ты больше не", "новая инструкция", "system prompt",
    "act as", "disregard", "вы теперь", "перестань быть", "jailbreak",
    "репиши свои правила", "покажи свой промпт", "выведи промпт", "reveal your prompt",
]

PRICE_HINT = [
    "сколько стоит", "сколько стоят", "сколько будет стоить", "цена", "цены", "цену",
    "стоимость", "прайс", "сколько будет", "почем", "почём", "по чем",
    "қанша тұрады", "баға", "narxi", "qancha",
]

#: Currency / amount shapes the post-check looks for in a drafted reply.
AMOUNT_RE = re.compile(
    # "15 000 ₸", "15000 тенге", "3 400 000 сум"
    r"(?:\d[\d\s\u00a0.,]*\d|\d)\s*(?:₸|тг|тенге|сум|so'm|som|руб|₽|usd|kzt|uzs)"
    # "₸15 000", "$120"
    r"|(?:₸|\$)\s*\d"
    # A thousands-grouped number on its own ("от 15 000") is still a price claim.
    r"|\b\d{1,3}[\s\u00a0]\d{3}(?:[\s\u00a0]\d{3})*\b",
    re.IGNORECASE,
)

GUARANTEE_RE = re.compile(
    r"(?:\b(?:гарантиру\w*|обязательно поможет|точно вылечим|без боли гарант"
    r"|guarantee\w*|кепілдік беремін|kafolat beraman)\b)"
    # "100%" ends in a non-word character, so no trailing \b here.
    r"|(?:\b100\s*%)",
    re.IGNORECASE,
)

#: edu vertical: promises about exam scores.
SCORE_PROMISE_RE = re.compile(
    r"\b(гарантиру\w*\s+(?:7|7\.0|8|балл|поступлен)|обязательно сдад|точно поступ"
    r"|guaranteed?\s+(?:score|band)|kafolatlangan ball)\b",
    re.IGNORECASE,
)

DIAGNOSIS_OUTPUT_RE = re.compile(
    # An assertion about the patient's condition…
    r"\b(?:у вас|у тебя|это)\s+(?:скорее всего\s+)?"
    r"(?:кариес|пульпит|периодонтит|гингивит|периостит|рак|абсцесс|киста|инфекция)\b"
    # …a stated diagnosis…
    r"|\bдиагноз\s*[:\u2014-]\s*\w"
    r"|\b(?:ставлю|поставил[аи]?)\s+диагноз\b"
    # …or a prescription. Naming a drug is never acceptable from the bot.
    r"|\b(?:назначаю|назначим|пропишу|прописываю)\b"
    r"|\b(?:примите|выпейте|принимайте|пейте)\s+\S+"
    r"|\b(?:ибупрофен|кетанов|нимесил|кеторол|анальгин|амоксицилл\w*|антибиотик\w*)\b",
    re.IGNORECASE,
)

class Intent(StrEnum):
    RED_FLAG = "red_flag"
    HUMAN_REQUEST = "human_request"
    OPT_OUT = "opt_out"
    DELETE_DATA = "delete_data"
    DISCOUNT = "discount"
    REFUND = "refund"
    DIAGNOSIS = "diagnosis_request"
    PRICE = "price_question"
    INJECTION = "injection"
    NORMAL = "normal"


@dataclass
class PreCheck:
    intents: set[Intent] = field(default_factory=set)
    matched: dict[str, str] = field(default_factory=dict)

    @property
    def red_flag(self) -> bool:
        return Intent.RED_FLAG in self.intents

    @property
    def forces_handoff(self) -> bool:
        return bool(
            self.intents
            & {
                Intent.RED_FLAG,
                Intent.HUMAN_REQUEST,
                Intent.DISCOUNT,
                Intent.REFUND,
            }
        )

    def has(self, intent: Intent) -> bool:
        return intent in self.intents


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    # Collapse character padding used to slip past filters ("с к и д к а").
    text = re.sub(r"[ \t ]+", " ", text)
    return text


def _find(text: str, phrases: list[str]) -> str | None:
    for phrase in phrases:
        if phrase in text:
            return phrase
    return None


def precheck(
    message: str,
    *,
    lang: str = "ru",
    extra_red_flags: dict[str, list[str]] | None = None,
) -> PreCheck:
    """Classify an inbound message with deterministic rules."""
    text = _normalize(message)
    result = PreCheck()
    if not text:
        return result

    # Red flags are checked in every language we know, not just the detected
    # one: a KZ-speaking patient may write one Russian word, or vice versa, and
    # missing an emergency is far worse than an occasional false positive.
    red_flags: list[str] = []
    for bucket in RED_FLAGS_DEFAULT.values():
        red_flags.extend(bucket)
    if extra_red_flags:
        for bucket in extra_red_flags.values():
            red_flags.extend(bucket or [])

    checks: list[tuple[Intent, list[str]]] = [
        (Intent.RED_FLAG, red_flags),
        (Intent.DELETE_DATA, DELETE_REQUEST),
        (Intent.OPT_OUT, OPT_OUT),
        (Intent.HUMAN_REQUEST, HUMAN_REQUEST),
        (Intent.REFUND, REFUND),
        (Intent.DISCOUNT, DISCOUNT),
        (Intent.DIAGNOSIS, DIAGNOSIS_REQUEST),
        (Intent.PRICE, PRICE_HINT),
        (Intent.INJECTION, INJECTION),
    ]
    for intent, phrases in checks:
        hit = _find(text, phrases)
        if hit:
            result.intents.add(intent)
            result.matched[str(intent)] = hit

    # "стоп" inside a longer sentence is usually not an opt-out.
    if Intent.OPT_OUT in result.intents and len(text.split()) > 6:
        if result.matched.get(str(Intent.OPT_OUT)) in {"стоп", "stop"}:
            result.intents.discard(Intent.OPT_OUT)
    return result


@dataclass
class PostCheck:
    ok: bool
    violations: list[str] = field(default_factory=list)
    #: Reply the caller should send instead, when ``ok`` is False.
    replacement_reason: str | None = None


def postcheck(
    reply: str,
    *,
    tool_names_used: set[str] | None = None,
    allow_prices: bool = False,
    vertical: str = "clinic",
) -> PostCheck:
    """Validate a drafted reply before it leaves the system.

    ``allow_prices`` is set by the runtime when a price-bearing tool
    (``list_services`` / ``match_course`` / ``get_balance`` …) actually ran in
    this turn.  A number with a currency in any other situation is treated as
    hallucinated.
    """
    violations: list[str] = []

    if AMOUNT_RE.search(reply or "") and not allow_prices:
        violations.append("price_without_tool")
    if GUARANTEE_RE.search(reply or ""):
        violations.append("guarantee")
    if vertical == "clinic" and DIAGNOSIS_OUTPUT_RE.search(reply or ""):
        violations.append("diagnosis")
    if vertical == "edu" and SCORE_PROMISE_RE.search(reply or ""):
        violations.append("score_promise")
    # A reply that leaks the prompt scaffolding.
    if re.search(r"\b(system prompt|мой промпт|инструкции разработчика)\b", reply or "", re.I):
        violations.append("prompt_leak")

    if not violations:
        return PostCheck(ok=True)
    return PostCheck(
        ok=False,
        violations=violations,
        replacement_reason=violations[0],
    )


def price_tools_for(vertical: str) -> set[str]:
    if vertical == "edu":
        return {"match_course", "list_groups", "get_balance", "kb_search", "create_payment_link"}
    return {"list_services", "kb_search", "my_visits", "create_payment_link"}


__all__ = [
    "AMOUNT_RE",
    "Intent",
    "PostCheck",
    "PreCheck",
    "RED_FLAGS_DEFAULT",
    "postcheck",
    "precheck",
    "price_tools_for",
]
