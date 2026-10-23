"""Passwords, device tokens, session JWTs, TOTP and rate limiting.

Credential handling is identical for console sessions and the legacy compatibility
layer; there is exactly one implementation of each primitive.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import deque

import bcrypt
import jwt
import pyotp

from .config import resolve_version

__all__ = [
    "DEVICE_TOKEN_BYTES",
    "DEVICE_TOKEN_HEADER",
    "JWT_ALGORITHM",
    "SESSION_COOKIE",
    "RateLimiter",
    "constant_time_compare",
    "decode_session",
    "device_token_hash",
    "hash_password",
    "issue_session",
    "new_device_token",
    "new_totp_secret",
    "totp_uri",
    "verify_password",
    "verify_totp",
]

JWT_ALGORITHM = "HS256"
DEVICE_TOKEN_BYTES = 32
SESSION_COOKIE = "obs_session"
DEVICE_TOKEN_HEADER = "X-Device-Token"


# ---------------------------------------------------------------- passwords ---
def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def constant_time_compare(left: str, right: str) -> bool:
    """Timing-safe comparison for shared codes and tokens."""

    return hmac.compare_digest((left or "").encode("utf-8"), (right or "").encode("utf-8"))


# ------------------------------------------------------------- device tokens ---
def new_device_token() -> str:
    """Fresh URL-safe device token; only its keyed hash is persisted."""

    return base64.urlsafe_b64encode(secrets.token_bytes(DEVICE_TOKEN_BYTES)).decode("ascii").rstrip("=")


def device_token_hash(token: str, secret_key: str) -> str:
    """Keyed hash so a leaked database alone cannot be replayed as a token."""

    if not token:
        return ""
    digest = hmac.new(secret_key.encode("utf-8"), token.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


# --------------------------------------------------------------- session JWT ---
def issue_session(
    *,
    secret_key: str,
    ttl_minutes: int,
    version: str | None = None,
    actor: str = "admin",
) -> tuple[str, int]:
    """Return ``(token, expires_at_ms)`` for a console session."""

    now = int(time.time())
    expires_at_s = now + ttl_minutes * 60
    payload = {
        "sub": actor,
        "scope": "console",
        "iat": now,
        "exp": expires_at_s,
        "ver": version or resolve_version(),
    }
    token = jwt.encode(payload, secret_key, algorithm=JWT_ALGORITHM)
    return token, expires_at_s * 1000


def decode_session(token: str, *, secret_key: str) -> dict | None:
    """Decode a console session JWT; ``None`` for anything invalid or expired."""

    if not token:
        return None
    try:
        payload = jwt.decode(token, secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("scope") != "console":
        return None
    return payload


# --------------------------------------------------------------------- TOTP ---
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, *, account: str = "admin", issuer: str = "Observatory") -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    cleaned = (code or "").strip().replace(" ", "")
    if not secret or not cleaned.isdigit():
        return False
    # One 30 s step of clock skew is tolerated.
    return pyotp.TOTP(secret).verify(cleaned, valid_window=1)


# -------------------------------------------------------------- rate limiter ---
class RateLimiter:
    """In-memory sliding-window limiter (one process, no Redis dependency)."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1, int(window_seconds))
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return bucket

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._prune(key, now)
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        bucket = self._prune(key, now)
        if len(bucket) < self.limit:
            return 0
        return max(1, int(self.window - (now - bucket[0])) + 1)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def clear(self) -> None:
        self._hits.clear()