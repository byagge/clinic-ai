"""Channel abstraction.

A channel adapter does two things and nothing else: turn a provider webhook
into an :class:`InboundMessage`, and deliver an :class:`OutboundMessage`.  All
business logic lives above this layer, so adding Instagram later means adding
one file.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class InboundMessage:
    """Normalised inbound message."""

    channel_type: str
    #: Where to reply (tg chat id, wa jid).
    peer_id: str
    #: Provider-unique message id; used for webhook-replay deduplication.
    external_id: str
    text: str | None = None
    type: str = "text"
    sender_name: str | None = None
    phone: str | None = None
    media_url: str | None = None
    #: Raw media bytes for voice notes we transcribe ourselves.
    media_bytes: bytes | None = None
    media_mime: str | None = None
    #: Telegram Business connection, when the message arrived through a
    #: business account rather than the bot's own chat.
    business_connection_id: str | None = None
    #: Button payload (callback data) when the patient tapped a button.
    callback_data: str | None = None
    #: Set when we must answer a callback query to clear the spinner.
    callback_id: str | None = None
    #: Provider user id of the author (Telegram user id, WhatsApp jid).
    from_user_id: str | None = None
    #: True when the author is the business/клиника side, not the patient.
    #: In Telegram Business and Baileys, our own and the owner's manual
    #: messages are echoed back to us; those must pause the bot, not feed it.
    from_business: bool = False
    timestamp: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_button(self) -> bool:
        return bool(self.callback_data)


@dataclass
class Button:
    text: str
    data: str


@dataclass
class OutboundMessage:
    peer_id: str
    text: str
    buttons: list[Button] = field(default_factory=list)
    business_connection_id: str | None = None
    #: WhatsApp Cloud API template, used outside the 24h window.
    template: dict | None = None
    #: Links the delivery back to a stored message row.
    message_id: uuid.UUID | None = None


@dataclass
class DeliveryResult:
    ok: bool
    external_id: str | None = None
    error: str | None = None
    #: True when the provider charged for this message (Cloud API templates).
    billable: bool = False
    #: Set when the channel needs attention (session logged out, token revoked).
    channel_broken: bool = False


class ChannelAdapter(Protocol):
    type: str

    async def send(self, message: OutboundMessage) -> DeliveryResult: ...

    async def health(self) -> dict: ...


class ChannelError(Exception):
    pass


__all__ = [
    "Button",
    "ChannelAdapter",
    "ChannelError",
    "DeliveryResult",
    "InboundMessage",
    "OutboundMessage",
]
