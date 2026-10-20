"""LLM brain — Anthropic tool-use loop, plus a deterministic fallback.

Two implementations share one interface:

* :class:`AnthropicBrain` — Claude with tool use. Sonnet-class for dialogue,
  Haiku-class for classification/summaries.
* :class:`ScriptedBrain` — a rule-based state machine that calls the *same*
  tools.  It runs when no API key is configured, which makes ``docker compose
  up`` demoable offline and gives the eval suite a deterministic baseline.

The loop lives here so both brains obey the same tool-round cap and the same
"a tool result is the only source of facts" discipline.
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from arix.config import get_settings
from arix.logging_setup import get_logger

log = get_logger("arix.brain")

ToolExecutor = Callable[[str, dict], Awaitable[dict]]


@dataclass
class BrainResult:
    text: str
    tool_calls: list[tuple[str, dict, dict]] = field(default_factory=list)  # name, args, result
    rounds: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    model: str = "scripted"
    stop_reason: str | None = None

    @property
    def tool_names(self) -> set[str]:
        return {name for name, _, _ in self.tool_calls}


class Brain(Protocol):
    async def run(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        execute: ToolExecutor,
        state: dict | None = None,
    ) -> BrainResult: ...


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class AnthropicBrain:
    def __init__(self, model: str | None = None, max_tokens: int | None = None) -> None:
        s = get_settings()
        self.model = model or s.llm_dialog_model
        self.max_tokens = max_tokens or s.llm_max_tokens
        self.max_rounds = s.llm_max_tool_rounds
        self._client = None

    def _client_or_create(self):
        if self._client is None:
            from anthropic import AsyncAnthropic

            s = get_settings()
            self._client = AsyncAnthropic(api_key=s.anthropic_api_key, timeout=s.llm_timeout_s)
        return self._client

    async def run(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        execute: ToolExecutor,
        state: dict | None = None,
    ) -> BrainResult:
        # ``state`` is already folded into the system prompt by the runtime;
        # the model does not need it a second time.
        client = self._client_or_create()
        convo: list[dict] = [dict(m) for m in messages]
        result = BrainResult(text="", model=self.model)

        for round_index in range(self.max_rounds):
            response = await client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=tools or None,
                messages=convo,
            )
            result.rounds = round_index + 1
            result.stop_reason = response.stop_reason
            usage = getattr(response, "usage", None)
            if usage is not None:
                result.usage["input_tokens"] = result.usage.get("input_tokens", 0) + (
                    usage.input_tokens or 0
                )
                result.usage["output_tokens"] = result.usage.get("output_tokens", 0) + (
                    usage.output_tokens or 0
                )

            text_parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
            tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]

            if text_parts:
                result.text = "\n".join(t.strip() for t in text_parts if t.strip()).strip()

            if not tool_uses:
                break

            convo.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in tool_uses:
                args = block.input if isinstance(block.input, dict) else {}
                try:
                    out = await execute(block.name, args)
                except Exception as exc:  # a broken tool must not kill the dialogue
                    log.exception("tool_failed", tool=block.name)
                    out = {"error": "tool_failed", "detail": str(exc)[:200]}
                result.tool_calls.append((block.name, args, out))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(out, ensure_ascii=False, default=str),
                    }
                )
            convo.append({"role": "user", "content": tool_results})
        return result

    async def classify(self, system: str, text: str, schema_hint: str) -> dict[str, Any]:
        """One-shot JSON classification on the fast model."""
        client = self._client_or_create()
        s = get_settings()
        response = await client.messages.create(
            model=s.llm_fast_model,
            max_tokens=400,
            system=f"{system}\n\nОтвечай ТОЛЬКО JSON по схеме: {schema_hint}",
            messages=[{"role": "user", "content": text}],
        )
        raw = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        return _loose_json(raw)

    async def summarize(self, system: str, text: str, max_tokens: int = 300) -> str:
        client = self._client_or_create()
        s = get_settings()
        response = await client.messages.create(
            model=s.llm_fast_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": text}],
        )
        return "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()


def _loose_json(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def build_brain(playbook=None) -> Brain:
    """Claude when a key is configured, the deterministic brain otherwise.

    ``playbook`` is the vertical's dialogue flow; it is required only for the
    deterministic path, which is why the vertical passes it in rather than this
    module importing it (see docs/CORE_SYNC.md).
    """
    from .scripted import ScriptedBrain

    s = get_settings()
    if s.llm_enabled:
        return AnthropicBrain()
    if playbook is None:
        raise RuntimeError(
            "no ANTHROPIC_API_KEY and no playbook: pass the vertical's playbook "
            "to build_brain(), or configure a key"
        )
    return ScriptedBrain(playbook)


__all__ = ["AnthropicBrain", "Brain", "BrainResult", "build_brain"]
