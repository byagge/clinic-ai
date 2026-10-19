"""Speech to text for voice notes.

Whisper-class API by default; ``STT_PROVIDER=local`` points at a self-hosted
faster-whisper service for KZ deployments that must keep audio in-country.
Returns ``None`` on any failure — a missed transcription degrades to "передаю
администратору", it never drops the message.
"""
from __future__ import annotations

import httpx

from arix.channels.base import InboundMessage
from arix.config import get_settings
from arix.logging_setup import get_logger

log = get_logger("arix.stt")


async def transcribe(inbound: InboundMessage) -> str | None:
    s = get_settings()
    if s.stt_provider == "none":
        return None
    audio = inbound.media_bytes
    if audio is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                f"{s.stt_base_url.rstrip('/')}/audio/transcriptions",
                headers={"Authorization": f"Bearer {s.stt_api_key}"} if s.stt_api_key else {},
                files={"file": ("voice.ogg", audio, inbound.media_mime or "audio/ogg")},
                data={"model": s.stt_model, "response_format": "json"},
            )
            if response.status_code != 200:
                log.warning("stt_failed", status=response.status_code)
                return None
            return (response.json().get("text") or "").strip() or None
    except Exception as exc:  # pragma: no cover - network
        log.warning("stt_error", error=str(exc))
        return None


__all__ = ["transcribe"]
