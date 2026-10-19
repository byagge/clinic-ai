"""Tool framework shared by every vertical."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.pii import PIIVault
from arix.db.models_core import Contact, Conversation


@dataclass
class OutboundIntent:
    """Something the runtime must do after the model finished reasoning."""

    kind: str  # "handoff" | "notice" | "quick_replies" | "job"
    payload: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    session: AsyncSession
    ctx: TenantContext
    conversation: Conversation
    contact: Contact
    lang: str
    vault: PIIVault
    now: datetime
    #: Filled by tools, consumed by the runtime (handoffs, buttons, jobs).
    intents: list[OutboundIntent] = field(default_factory=list)
    #: Short-lived memory for one turn, e.g. the slot list the agent offered.
    turn_state: dict[str, Any] = field(default_factory=dict)

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.ctx.id

    def request_handoff(self, reason: str, priority: str = "normal", summary: str | None = None):
        self.intents.append(
            OutboundIntent(
                "handoff", {"reason": reason, "priority": priority, "summary": summary}
            )
        )

    def add_buttons(self, buttons: list[dict]) -> None:
        self.intents.append(OutboundIntent("quick_replies", {"buttons": buttons}))


ToolHandler = Callable[[ToolContext, dict], Awaitable[dict]]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict
    handler: ToolHandler
    #: Tools whose output may legitimately contain money.
    provides_prices: bool = False
    #: The runtime stops the tool loop after a terminal tool.
    terminal: bool = False
    #: Writes data — never retried automatically by the loop.
    mutating: bool = False

    def as_anthropic(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> ToolDef:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def extend(self, tools: list[ToolDef]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        return [t.as_anthropic() for t in self._tools.values()]

    def price_tools(self) -> set[str]:
        return {t.name for t in self._tools.values() if t.provides_prices}

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())


def tool(
    name: str,
    description: str,
    schema: dict,
    *,
    provides_prices: bool = False,
    terminal: bool = False,
    mutating: bool = False,
):
    """Decorator that turns an async function into a :class:`ToolDef`."""

    def wrapper(fn: ToolHandler) -> ToolDef:
        return ToolDef(
            name=name,
            description=description,
            input_schema=schema,
            handler=fn,
            provides_prices=provides_prices,
            terminal=terminal,
            mutating=mutating,
        )

    return wrapper


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


S_STRING = {"type": "string"}
S_INT = {"type": "integer"}


__all__ = [
    "OutboundIntent",
    "S_INT",
    "S_STRING",
    "ToolContext",
    "ToolDef",
    "ToolRegistry",
    "obj",
    "tool",
]
