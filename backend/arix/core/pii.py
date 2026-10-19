"""PII Guard — masks personal data before it reaches the LLM.

Phone numbers, national ids (ИИН/ЖШС/ПИНФЛ), e-mails, card numbers and known
contact names are replaced with stable tokens such as ``[PHONE_1]``.  The same
vault restores them in the model's reply, so the patient still sees their real
name while the provider only ever sees tokens.

The guard is deliberately conservative: it masks a superset of what is strictly
required (a false positive only costs readability inside the prompt), and it is
pure-Python so it can be unit-tested exhaustively.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Order matters: longer/more specific patterns first so that an e-mail is not
# partially eaten by the phone pattern.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    # KZ ИИН / UZ ПИНФЛ: 12 or 14 standalone digits.
    ("NATID", re.compile(r"(?<!\d)(?:\d{12}|\d{14})(?!\d)")),
    # International and local phone shapes: +7 777 123 45 67, 8(777)1234567, …
    (
        "PHONE",
        re.compile(
            r"(?<![\w])(?:\+?\d{1,3}[\s\-.]?)?(?:\(\d{2,4}\)|\d{2,4})"
            r"(?:[\s\-.]?\d{2,4}){2,4}(?![\w])"
        ),
    ),
)

#: A digit run shorter than this is never treated as a phone (prices, times).
MIN_PHONE_DIGITS = 9


@dataclass
class PIIVault:
    """Holds the token ↔ original mapping for a single LLM round-trip."""

    forward: dict[str, str] = field(default_factory=dict)  # original -> token
    backward: dict[str, str] = field(default_factory=dict)  # token -> original
    _counters: dict[str, int] = field(default_factory=dict)

    def token_for(self, kind: str, original: str) -> str:
        if original in self.forward:
            return self.forward[original]
        self._counters[kind] = self._counters.get(kind, 0) + 1
        token = f"[{kind}_{self._counters[kind]}]"
        self.forward[original] = token
        self.backward[token] = original
        return token

    def restore(self, text: str) -> str:
        if not text:
            return text
        for token, original in self.backward.items():
            text = text.replace(token, original)
        return text

    @property
    def is_empty(self) -> bool:
        return not self.backward


def _digit_count(value: str) -> int:
    return sum(ch.isdigit() for ch in value)


def mask_text(
    text: str | None,
    vault: PIIVault | None = None,
    *,
    extra_names: list[str] | None = None,
    enabled: bool = True,
) -> tuple[str, PIIVault]:
    """Return ``(masked_text, vault)``.

    ``extra_names`` lets the caller mask names that only the database knows
    (the contact's own name), which no regex could find reliably.
    """
    vault = vault or PIIVault()
    if not text:
        return text or "", vault
    if not enabled:
        return text, vault

    masked = text
    for kind, pattern in PATTERNS:

        def _sub(match: re.Match[str], kind: str = kind) -> str:
            raw = match.group(0)
            if kind in ("PHONE", "CARD") and _digit_count(raw) < MIN_PHONE_DIGITS:
                return raw
            stripped = raw.strip()
            lead = raw[: len(raw) - len(raw.lstrip())]
            trail = raw[len(raw.rstrip()) :]
            return f"{lead}{vault.token_for(kind, stripped)}{trail}"

        masked = pattern.sub(_sub, masked)

    for name in sorted(filter(None, extra_names or []), key=len, reverse=True):
        if len(name) < 3:
            continue
        masked = re.sub(
            rf"\b{re.escape(name)}\b", lambda _m, n=name: vault.token_for("NAME", n), masked,
            flags=re.IGNORECASE,
        )
    return masked, vault


def normalize_phone(raw: str | None, default_country: str = "KZ") -> str | None:
    """Best-effort E.164 normalisation for KZ/UZ/RU numbers."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    prefixes = {"KZ": "7", "RU": "7", "UZ": "998"}
    cc = prefixes.get(default_country.upper(), "7")
    if raw.strip().startswith("+"):
        return f"+{digits}"
    if cc == "998":
        if len(digits) == 9:
            return f"+998{digits}"
        if digits.startswith("998"):
            return f"+{digits}"
    else:
        if len(digits) == 10:
            return f"+7{digits}"
        if len(digits) == 11 and digits[0] in "78":
            return f"+7{digits[1:]}"
    if len(digits) >= 11:
        return f"+{digits}"
    return f"+{cc}{digits}"


def looks_like_phone(text: str) -> bool:
    return _digit_count(text) >= MIN_PHONE_DIGITS and bool(
        PATTERNS[-1][1].search(text)
    )


__all__ = ["PIIVault", "looks_like_phone", "mask_text", "normalize_phone"]
