"""Password hashing, device tokens, JWTs, TOTP and rate limiting."""

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

__all__ = [
    "RateLimiter",
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


# ------------------------------------------------------------- device tokens ---
def new_device_token() -> str:
    """Return a fresh, URL-safe device token (only the hash is persisted)."""

    return base64.urlsafe_b64encode(secrets.token_bytes(DEVICE_TOKEN_BYTES)).decode("ascii").rstrip("=")


def device_token_hash(token: str, secret_key: str) -> str:
    """Keyed hash so a leaked database alone cannot be replayed as device tokens."""

    if not token:
        return ""
    digest = hmac.new(secret_key.encode("utf-8"), token.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


# --------------------------------------------------------------- admin JWT ---
def issue_session(*, secret_key: str, ttl_minutes: int, version: str, actor: str = "admin") -> tuple[str, int]:
    now = int(time.time())
    expires_at_s = now + ttl_minutes * 60
    payload = {
        "sub": actor,
        "scope": "console",
        "iat": now,
        "exp": expires_at_s,
        "ver": version,
    }
    token = jwt.encode(payload, secret_key, algorithm=JWT_ALGORITHM)
    return token, expires_at_s * 1000


def decode_session(token: str, *, secret_key: str) -> dict | None:
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
    # valid_window=1 tolerates a single 30s step of clock skew.
    return pyotp.TOTP(secret).verify(cleaned, valid_window=1)


# -------------------------------------------------------------- rate limiter ---
class RateLimiter:
    """In-memory sliding-window limiter.

    One process serves the console, so a dictionary of deques is enough and
    avoids a Redis dependency. Keys are strings such as ``"login:1.2.3.4"``.
    """

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = max(1, limit)
        self.window = max(1, window_seconds)
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