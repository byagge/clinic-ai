"""Password hashing for panel logins.

Uses ``bcrypt`` directly rather than passlib: passlib 1.7's bcrypt backend
breaks against bcrypt >= 4.1, and we need exactly two functions.

bcrypt silently ignores anything past 72 bytes, so long passphrases are
pre-hashed with SHA-256 and base64-encoded first.  That keeps the full entropy
and makes the input a fixed, safe length.
"""
from __future__ import annotations

import base64
import hashlib
import secrets

import bcrypt

ROUNDS = 12
#: A hash of a throwaway value, used to keep timing constant for unknown users.
_DUMMY = bcrypt.hashpw(b"arix-dummy", bcrypt.gensalt(rounds=4))


def _prepare(password: str) -> bytes:
    raw = (password or "").encode("utf-8")
    if len(raw) <= 72:
        return raw
    return base64.b64encode(hashlib.sha256(raw).digest())


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt(rounds=ROUNDS)).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    """Constant-ish time check that also runs when the user does not exist."""
    candidate = (hashed or "").encode() or _DUMMY
    try:
        return bcrypt.checkpw(_prepare(password), candidate)
    except (ValueError, TypeError):
        # Malformed stored hash: still burn a comparison, then fail.
        bcrypt.checkpw(_prepare(password), _DUMMY)
        return False


def random_password(length: int = 12) -> str:
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


__all__ = ["hash_password", "random_password", "verify_password"]
