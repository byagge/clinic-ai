"""Outbound message templates.

Resolution order for every message the system sends unprompted:

1. ``tenant_config.texts[code][lang]`` — what the clinic wrote itself;
2. ``DEFAULTS[code][lang]`` — ARIX defaults shipped with the template pack;
3. ``DEFAULTS[code]["ru"]`` — last resort.

Placeholders use ``str.format`` with a forgiving mapping, so a template that
references an unknown field renders the field name instead of raising in
production.
"""
from __future__ import annotations

import re
from typing import Any

from .defaults import DEFAULTS


class _Forgiving(dict):
    def __missing__(self, key: str) -> str:  # pragma: no cover - defensive
        return "{" + key + "}"


def resolve(code: str, lang: str, overrides: dict[str, dict[str, str]] | None = None) -> str | None:
    overrides = overrides or {}
    bucket = overrides.get(code) or {}
    for candidate in (bucket.get(lang), bucket.get("ru"), *(
        (DEFAULTS.get(code) or {}).get(key) for key in (lang, "ru")
    )):
        if candidate:
            return candidate
    return None


def render(
    code: str,
    lang: str,
    *,
    overrides: dict[str, dict[str, str]] | None = None,
    **values: Any,
) -> str:
    template = resolve(code, lang, overrides)
    if template is None:
        raise KeyError(f"no template for {code!r}")
    text = template.format_map(_Forgiving(values))
    # Collapse artefacts left by empty optional values.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # A name that already ends in "." must not produce "Айгерим Б.." .
    text = re.sub(r"\.\.(?!\.)", ".", text)
    return text.strip()


def has(code: str) -> bool:
    return code in DEFAULTS


__all__ = ["DEFAULTS", "has", "render", "resolve"]
