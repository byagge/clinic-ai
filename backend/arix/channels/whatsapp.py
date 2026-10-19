"""WhatsApp adapters.

**Baileys (default).** A Node sidecar (``waweb/``) speaks the WhatsApp Web
multi-device protocol, so a clinic links its existing WhatsApp number by
scanning a QR code once.  There is no per-message fee and no template
approval, which is the point: a small clinic should not pay Meta to send
"ждём вас завтра в 10:00".

Trade-offs we accept and document for the client (docs/WHATSAPP_BAILEYS.md):
unofficial client, so sensible send pacing, no bulk blasting, and a Cloud API
path kept ready for tenants that need the official channel.

**Cloud API (optional).** Same interface, used when a tenant provides Meta
credentials.  Outside the 24-hour service window it can only send approved
templates, and those messages are counted for the free-tier alert.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from arix.config import get_settings
from arix.logging_setup import get_logger

from .base import Button, DeliveryResult, InboundMessage, OutboundMessage

log = get_logger("arix.whatsapp")


def jid_to_phone(jid: str) -> str | None:
    match = re.match(r"^(\d{8,15})@", jid or "")
    return f"+{match.group(1)}" if match else None


def phone_to_jid(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return f"{digits}@s.whatsapp.net"


class BaileysAdapter:
    """Talks to the ``waweb`` sidecar over its small REST API."""

    type = "wa"

    def __init__(
        self,
        session_id: str,
        *,
        base_url: str | None = None,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        s = get_settings()
        self.session_id = session_id
        self.base_url = (base_url or s.wa_baileys_url).rstrip("/")
        self.token = token or s.wa_baileys_token
        self._client = client

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(self, method: str, path: str, **kwargs) -> tuple[int, dict]:
        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            response = await client.request(
                method, f"{self.base_url}{path}", headers=self._headers(), **kwargs
            )
            try:
                return response.status_code, response.json()
            except ValueError:
                return response.status_code, {"raw": response.text[:500]}
        finally:
            if self._client is None:
                await client.aclose()

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        payload: dict[str, Any] = {
            "to": message.peer_id,
            "text": message.text,
        }
        if message.buttons:
            # WhatsApp Web does not give us reliable interactive buttons, so we
            # append numbered choices and let the patient reply with a number.
            payload["text"] = _with_numbered_options(message.text, message.buttons)
            payload["options"] = [{"text": b.text, "data": b.data} for b in message.buttons]
        status, data = await self._request(
            "POST", f"/sessions/{self.session_id}/messages", json=payload
        )
        if status == 200 and data.get("ok"):
            return DeliveryResult(ok=True, external_id=data.get("id"))
        error = str(data.get("error") or f"http_{status}")
        return DeliveryResult(
            ok=False,
            error=error,
            channel_broken=status in (404, 409) or "logged_out" in error or "not_connected" in error,
        )

    async def start(self) -> dict:
        _, data = await self._request("POST", f"/sessions/{self.session_id}/start")
        return data

    async def logout(self) -> dict:
        _, data = await self._request("POST", f"/sessions/{self.session_id}/logout")
        return data

    async def qr(self) -> dict:
        """Returns ``{"status": "qr"|"connected"|..., "qr": "<data-url>"}``."""
        _, data = await self._request("GET", f"/sessions/{self.session_id}/qr")
        return data

    async def health(self) -> dict:
        status, data = await self._request("GET", f"/sessions/{self.session_id}")
        return {"ok": status == 200 and data.get("status") == "connected", **data}


class CloudApiAdapter:
    """Meta WhatsApp Cloud API — official, paid, template-gated."""

    type = "wa"

    def __init__(
        self,
        phone_number_id: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.phone_number_id = phone_number_id
        self.token = token
        self.version = get_settings().wa_cloud_api_version
        self._client = client

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        url = f"https://graph.facebook.com/{self.version}/{self.phone_number_id}/messages"
        to = re.sub(r"\D", "", message.peer_id)
        if message.template:
            body = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": message.template,
            }
            billable = True
        else:
            body = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": message.text, "preview_url": False},
            }
            billable = False
        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            response = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {self.token}"}
            )
            data = response.json() if response.content else {}
            if response.status_code == 200:
                ids = data.get("messages") or [{}]
                return DeliveryResult(
                    ok=True, external_id=ids[0].get("id"), billable=billable
                )
            error = str((data.get("error") or {}).get("message") or response.status_code)
            return DeliveryResult(
                ok=False, error=error, channel_broken=response.status_code in (401, 403)
            )
        finally:
            if self._client is None:
                await client.aclose()

    async def health(self) -> dict:
        return {"ok": True, "mode": "cloud_api", "phone_number_id": self.phone_number_id}


def _with_numbered_options(text: str, buttons: list[Button]) -> str:
    lines = [text.rstrip()]
    for index, button in enumerate(buttons, start=1):
        lines.append(f"{index}. {button.text}")
    lines.append("Ответьте номером варианта.")
    return "\n".join(lines)


def parse_baileys_webhook(payload: dict) -> list[InboundMessage]:
    """Parse the sidecar's normalised ``messages.upsert`` payload."""
    out: list[InboundMessage] = []
    for item in payload.get("messages", []):
        jid = item.get("from") or ""
        if jid.endswith("@g.us") or jid == "status@broadcast":
            continue  # groups and status updates are out of scope
        kind = item.get("type") or "text"
        out.append(
            InboundMessage(
                channel_type="wa",
                peer_id=jid,
                external_id=f"wa:{item.get('id')}",
                text=item.get("text"),
                type=kind if kind in ("text", "voice", "image", "document", "video") else "text",
                sender_name=item.get("pushName"),
                phone=jid_to_phone(jid),
                media_url=item.get("mediaUrl"),
                media_mime=item.get("mimetype"),
                from_user_id=jid,
                from_business=bool(item.get("fromMe")),
                timestamp=datetime.fromtimestamp(item["timestamp"], tz=UTC)
                if item.get("timestamp")
                else datetime.now(UTC),
                raw=item,
            )
        )
    return out


def parse_cloud_webhook(payload: dict) -> list[InboundMessage]:
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            contacts = {c["wa_id"]: c for c in value.get("contacts", [])}
            for message in value.get("messages", []):
                wa_id = message.get("from")
                kind = message.get("type")
                text = None
                if kind == "text":
                    text = (message.get("text") or {}).get("body")
                elif kind == "button":
                    text = (message.get("button") or {}).get("text")
                elif kind == "interactive":
                    interactive = message.get("interactive") or {}
                    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                    text = reply.get("title")
                out.append(
                    InboundMessage(
                        channel_type="wa",
                        peer_id=wa_id,
                        external_id=f"wa:{message.get('id')}",
                        text=text,
                        type={"audio": "voice", "image": "image", "document": "document"}.get(
                            kind, "text"
                        ),
                        sender_name=(contacts.get(wa_id, {}).get("profile") or {}).get("name"),
                        phone=f"+{wa_id}" if wa_id else None,
                        media_url=(message.get(kind) or {}).get("id")
                        if kind in ("audio", "image", "document")
                        else None,
                        from_user_id=wa_id,
                        timestamp=datetime.fromtimestamp(int(message["timestamp"]), tz=UTC)
                        if message.get("timestamp")
                        else datetime.now(UTC),
                        raw=message,
                    )
                )
    return out


__all__ = [
    "BaileysAdapter",
    "CloudApiAdapter",
    "jid_to_phone",
    "parse_baileys_webhook",
    "parse_cloud_webhook",
    "phone_to_jid",
]
