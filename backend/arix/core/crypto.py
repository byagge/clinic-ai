"""Symmetric encryption for channel credentials at rest."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from arix.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    s = get_settings()
    key = s.credentials_encryption_key
    if not key:
        # Derive a stable key from secret_key so local/demo deployments work
        # out of the box.  Production must set CREDENTIALS_ENCRYPTION_KEY.
        digest = hashlib.sha256(s.secret_key.encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_json(payload: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(payload, ensure_ascii=False).encode()).decode()


def decrypt_json(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except (InvalidToken, ValueError):
        return {}


def sign_payload(secret: str, body: bytes) -> str:
    """HMAC-SHA256 hex digest, used for sidecar ↔ backend webhooks."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, provided: str | None) -> bool:
    if not provided:
        return False
    expected = sign_payload(secret, body)
    # Accept both bare hex and the "sha256=" prefix Meta uses.
    provided = provided.split("=", 1)[-1].strip()
    return hmac.compare_digest(expected, provided)


__all__ = ["decrypt_json", "encrypt_json", "sign_payload", "verify_signature"]
