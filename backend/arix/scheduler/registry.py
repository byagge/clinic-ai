"""Handler registry — the worker's dispatch table.

The vertical registers its own handlers and two small providers the core jobs
need (a slot hint for follow-up texts, a daily report builder).  This keeps
``arix/scheduler`` identical across repos.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .handlers_core import CORE_HANDLERS

_handlers: dict[str, Callable[..., Awaitable[dict]]] = dict(CORE_HANDLERS)
_slot_hint: Callable[..., Awaitable[str | None]] | None = None
_report_builder: Callable[..., Awaitable[Any]] | None = None
_daily_arms: list[Callable[..., Awaitable[None]]] = []


def register_handlers(handlers: dict[str, Callable[..., Awaitable[dict]]]) -> None:
    _handlers.update(handlers)


def register_slot_hint(provider: Callable[..., Awaitable[str | None]]) -> None:
    global _slot_hint
    _slot_hint = provider


def register_report_builder(builder: Callable[..., Awaitable[Any]]) -> None:
    global _report_builder
    _report_builder = builder


def register_daily(arm: Callable[..., Awaitable[None]]) -> None:
    """Vertical-specific jobs armed once a day. ``worker.py`` stays shared."""
    if arm not in _daily_arms:
        _daily_arms.append(arm)


def daily_arms() -> list[Callable[..., Awaitable[None]]]:
    return list(_daily_arms)


def handler_for(kind: str) -> Callable[..., Awaitable[dict]] | None:
    return _handlers.get(kind)


def known_kinds() -> list[str]:
    return sorted(_handlers)


def slot_hint_provider() -> Callable[..., Awaitable[str | None]] | None:
    return _slot_hint


def report_builder() -> Callable[..., Awaitable[Any]] | None:
    return _report_builder


__all__ = [
    "daily_arms",
    "handler_for",
    "known_kinds",
    "register_daily",
    "register_handlers",
    "register_report_builder",
    "register_slot_hint",
    "report_builder",
    "slot_hint_provider",
]
