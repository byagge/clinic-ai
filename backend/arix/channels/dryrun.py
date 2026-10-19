"""No-op channel used by tests, evals and offline demos.

Messages are still written to ``messages`` by the messaging layer, so a demo
or an eval can read exactly what the patient would have received without any
provider credentials.  Enable with ``CHANNELS_DRY_RUN=true`` or per channel
with ``settings.dry_run = true``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from arix.logging_setup import get_logger

from .base import DeliveryResult, OutboundMessage

log = get_logger("arix.channels.dryrun")


@dataclass
class Sent:
    peer_id: str
    text: str
    buttons: list[str] = field(default_factory=list)


class DryRunAdapter:
    type = "dryrun"

    #: Shared in-process outbox so tests can assert on deliveries.
    outbox: list[Sent] = []

    def __init__(self, label: str = "dryrun") -> None:
        self.label = label

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        DryRunAdapter.outbox.append(
            Sent(
                peer_id=message.peer_id,
                text=message.text,
                buttons=[b.text for b in message.buttons],
            )
        )
        log.info("dryrun_send", peer=message.peer_id, chars=len(message.text or ""))
        return DeliveryResult(ok=True, external_id=f"dry:{uuid.uuid4().hex[:12]}")

    async def health(self) -> dict:
        return {"ok": True, "mode": "dryrun", "sent": len(DryRunAdapter.outbox)}

    @classmethod
    def clear(cls) -> None:
        cls.outbox.clear()

    @classmethod
    def last(cls) -> Sent | None:
        return cls.outbox[-1] if cls.outbox else None

    @classmethod
    def texts(cls) -> list[str]:
        return [item.text for item in cls.outbox]


__all__ = ["DryRunAdapter", "Sent"]
