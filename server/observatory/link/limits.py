"""Link rate limiting helpers (spec §5.5: RATE → 429 + retryable).

Two buckets guard every carrier request:

* a token bucket (SHA-256 of the token, never the raw token) so one noisy device
  cannot starve the fleet;
* a per-IP bucket that applies to authenticated and unauthenticated traffic, so
  rotating random tokens from one address cannot dodge the limiter.

The stricter of the two wins; the 429 carries ``Retry-After`` (and the WS close
path logs the IP + retry delay before closing with 4429).
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


def ip_rate_key(ip: str) -> str:
    return f"link-ip:{ip or 'unknown'}"


def rate_key(token: str, ip: str) -> str:
    cleaned = (token or "").strip()
    if cleaned:
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
        return f"link:{digest}"
    return ip_rate_key(ip)


def enforce(limiter: Any, ip_limiter: Any, token: str, ip: str) -> None:
    """Raise :class:`AppError` RATE when the token or client IP exceeds its window."""

    retry_after = 0
    key = rate_key(token, ip)
    if limiter is not None and not limiter.allow(key):
        retry_after = max(retry_after, int(limiter.retry_after(key)))
    ip_key = ip_rate_key(ip)
    if ip_limiter is not None and not ip_limiter.allow(ip_key):
        retry_after = max(retry_after, int(ip_limiter.retry_after(ip_key)))
    if retry_after:
        raise AppError.rate(RATE_MESSAGE, retry_after=retry_after)


__all__ = ["RATE_MESSAGE", "client_ip", "enforce", "ip_rate_key", "rate_key"]