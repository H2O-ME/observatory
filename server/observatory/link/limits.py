"""Link rate limiting helpers (spec §5.5: RATE → 429 + retryable).

Both carriers share one limiter from ``app.state.link_limiter``. Keys are
derived from the token's SHA-256 (never the raw token) or the client IP when the
request is unauthenticated, so one noisy device cannot starve the fleet.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..core.config import Settings
from ..core.errors import AppError

RATE_MESSAGE = "链路请求过于频繁，请稍后再试"


def client_ip(headers: Any, client: Any, settings: Settings) -> str:
    """Client IP, honouring ``X-Forwarded-For`` only when explicitly trusted (A11)."""

    if getattr(settings, "trust_forwarded_for", False):
        forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
        if forwarded:
            return str(forwarded).split(",", 1)[0].strip() or "unknown"
    host = getattr(client, "host", None) if client is not None else None
    return str(host or "unknown")


def rate_key(token: str, ip: str) -> str:
    cleaned = (token or "").strip()
    if cleaned:
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
        return f"link:{digest}"
    return f"link-ip:{ip}"


def enforce(limiter: Any, token: str, ip: str) -> None:
    """Raise :class:`AppError` RATE when this token/IP exceeds its window."""

    if limiter is None:
        return
    if not limiter.allow(rate_key(token, ip)):
        raise AppError.rate(RATE_MESSAGE)


__all__ = ["RATE_MESSAGE", "client_ip", "enforce", "rate_key"]