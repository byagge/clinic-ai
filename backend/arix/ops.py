"""ARIX-internal ops alerts (dead jobs, broken channels, webhook errors)."""
from __future__ import annotations

import httpx

from arix.config import get_settings
from arix.logging_setup import get_logger

log = get_logger("arix.ops")


async def alert_ops(text: str) -> bool:
    """Send an alert to the ARIX ops Telegram chat. Never raises."""
    s = get_settings()
    if not (s.telegram_ops_bot_token and s.telegram_ops_chat_id):
        log.warning("ops_alert_unrouted", text=text[:400])
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{s.telegram_ops_bot_token}/sendMessage",
                json={
                    "chat_id": s.telegram_ops_chat_id,
                    "text": f"[{s.env}] {text}"[:4000],
                    "link_preview_options": {"is_disabled": True},
                },
            )
            return response.status_code == 200
    except Exception as exc:  # pragma: no cover - network
        log.warning("ops_alert_failed", error=str(exc))
        return False


__all__ = ["alert_ops"]
