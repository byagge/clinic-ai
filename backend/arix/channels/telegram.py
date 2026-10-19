"""Telegram adapter — regular bots and Telegram Business.

Telegram Business lets a clinic connect **its own personal/business Telegram
account** to our bot: patients keep writing to the clinic's human account and
the bot answers inside that chat.  The only mechanical difference is that
updates arrive as ``business_message`` and replies must carry
``business_connection_id``.  Everything else — slots, prompts, handoffs — is
identical, which is why this is handled here and nowhere else.

See docs/TELEGRAM_BUSINESS.md for the setup steps.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from arix.logging_setup import get_logger

from .base import Button, DeliveryResult, InboundMessage, OutboundMessage

log = get_logger("arix.telegram")

API = "https://api.telegram.org"


class TelegramAdapter:
    type = "tg"

    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.token = token
        self._client = client

    async def _post(self, method: str, payload: dict) -> dict:
        client = self._client or httpx.AsyncClient(timeout=20.0)
        try:
            response = await client.post(
                f"{API}/bot{self.token}/{method}",
                json={k: v for k, v in payload.items() if v is not None},
            )
            data = response.json()
            if not data.get("ok"):
                log.warning("telegram_api_error", method=method, detail=data.get("description"))
            return data
        finally:
            if self._client is None:
                await client.aclose()

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        payload: dict[str, Any] = {
            "chat_id": message.peer_id,
            "text": message.text,
            "business_connection_id": message.business_connection_id,
            "link_preview_options": {"is_disabled": True},
        }
        if message.buttons:
            payload["reply_markup"] = {
                "inline_keyboard": _keyboard(message.buttons),
            }
        data = await self._post("sendMessage", payload)
        if data.get("ok"):
            return DeliveryResult(
                ok=True, external_id=str(data["result"]["message_id"])
            )
        description = str(data.get("description") or "unknown")
        broken = any(
            marker in description.lower()
            for marker in ("unauthorized", "bot was blocked", "chat not found", "token")
        )
        return DeliveryResult(ok=False, error=description, channel_broken=broken)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        await self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def set_webhook(self, url: str, secret: str) -> dict:
        return await self._post(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret,
                "allowed_updates": [
                    "message",
                    "callback_query",
                    "business_connection",
                    "business_message",
                    "edited_business_message",
                ],
                "drop_pending_updates": False,
            },
        )

    async def health(self) -> dict:
        data = await self._post("getMe", {})
        return {"ok": bool(data.get("ok")), "bot": (data.get("result") or {}).get("username")}

    async def file_bytes(self, file_id: str) -> tuple[bytes | None, str | None]:
        """Download a voice note so we can transcribe it ourselves."""
        info = await self._post("getFile", {"file_id": file_id})
        if not info.get("ok"):
            return None, None
        path = info["result"]["file_path"]
        client = self._client or httpx.AsyncClient(timeout=60.0)
        try:
            response = await client.get(f"{API}/file/bot{self.token}/{path}")
            if response.status_code != 200:
                return None, None
            return response.content, response.headers.get("content-type")
        finally:
            if self._client is None:
                await client.aclose()


def _keyboard(buttons: list[Button]) -> list[list[dict]]:
    """Two buttons per row keeps labels readable on phones."""
    rows: list[list[dict]] = []
    for index in range(0, len(buttons), 2):
        rows.append(
            [
                {"text": b.text, "callback_data": b.data[:64]}
                for b in buttons[index : index + 2]
            ]
        )
    return rows


def parse_update(update: dict) -> InboundMessage | None:
    """Convert a Telegram update into an :class:`InboundMessage`.

    Returns ``None`` for updates we do not act on (service messages, edits of
    our own posts, connection events handled elsewhere).
    """
    if "callback_query" in update:
        query = update["callback_query"]
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        user = query.get("from") or {}
        return InboundMessage(
            channel_type="tg",
            peer_id=str(chat.get("id") or user.get("id") or ""),
            external_id=f"cb:{query.get('id')}",
            text=None,
            type="button",
            callback_data=query.get("data"),
            callback_id=str(query.get("id")),
            sender_name=_full_name(user),
            business_connection_id=message.get("business_connection_id"),
            from_user_id=str(user.get("id")) if user.get("id") else None,
            timestamp=datetime.now(UTC),
            raw=update,
        )

    for key, business in (("message", False), ("business_message", True)):
        if key not in update:
            continue
        message = update[key]
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        kind, text, media_ref, mime = _classify(message)
        # In a business chat, messages the clinic's own account sends are
        # echoed back to the bot.  ``outgoing`` marks them; the gateway turns
        # them into "a human took over" instead of patient input.
        outgoing = bool(business and message.get("outgoing"))
        return InboundMessage(
            channel_type="tg",
            peer_id=str(chat.get("id")),
            external_id=f"{chat.get('id')}:{message.get('message_id')}",
            text=text,
            type=kind,
            sender_name=_full_name(user),
            phone=(message.get("contact") or {}).get("phone_number"),
            media_url=media_ref,
            media_mime=mime,
            business_connection_id=message.get("business_connection_id"),
            from_user_id=str(user.get("id")) if user.get("id") else None,
            from_business=outgoing,
            timestamp=datetime.fromtimestamp(message.get("date", 0), tz=UTC)
            if message.get("date")
            else datetime.now(UTC),
            raw=update,
        )
    return None


def parse_business_connection(update: dict) -> dict | None:
    """Extract a ``business_connection`` event (clinic connected/disconnected)."""
    connection = update.get("business_connection")
    if not connection:
        return None
    return {
        "id": connection.get("id"),
        "user_id": (connection.get("user") or {}).get("id"),
        "username": (connection.get("user") or {}).get("username"),
        "enabled": bool(connection.get("is_enabled", True)),
        "can_reply": bool(
            (connection.get("rights") or {}).get("can_reply", connection.get("can_reply", True))
        ),
    }


def _classify(message: dict) -> tuple[str, str | None, str | None, str | None]:
    if message.get("voice"):
        voice = message["voice"]
        return "voice", None, voice.get("file_id"), voice.get("mime_type")
    if message.get("audio"):
        audio = message["audio"]
        return "voice", None, audio.get("file_id"), audio.get("mime_type")
    if message.get("photo"):
        largest = sorted(message["photo"], key=lambda p: p.get("file_size", 0))[-1]
        return "image", message.get("caption"), largest.get("file_id"), "image/jpeg"
    if message.get("document"):
        document = message["document"]
        return "document", message.get("caption"), document.get("file_id"), document.get("mime_type")
    if message.get("video") or message.get("video_note"):
        video = message.get("video") or message["video_note"]
        return "video", message.get("caption"), video.get("file_id"), video.get("mime_type")
    if message.get("location"):
        location = message["location"]
        return "location", f"{location.get('latitude')},{location.get('longitude')}", None, None
    if message.get("contact"):
        return "text", message["contact"].get("phone_number"), None, None
    return "text", message.get("text") or message.get("caption"), None, None


def _full_name(user: dict) -> str | None:
    parts = [user.get("first_name"), user.get("last_name")]
    name = " ".join(p for p in parts if p).strip()
    return name or user.get("username")


__all__ = ["TelegramAdapter", "parse_business_connection", "parse_update"]
