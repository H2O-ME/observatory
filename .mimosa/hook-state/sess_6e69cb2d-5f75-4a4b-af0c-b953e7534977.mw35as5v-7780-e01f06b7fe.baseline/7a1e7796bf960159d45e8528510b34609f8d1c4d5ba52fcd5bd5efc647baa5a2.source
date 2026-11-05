"""Server clock and the one timeout budget (C5, spec §5.4).

There is exactly one notion of "now" for the server: :func:`now_ms`. Devices may
report their own capture timestamp, but that value is always stored as
``device_ts_ms`` and never used as a server clock.

Timeout budgets are computed here, once. API/console code must not invent its
own timeout: it asks for the deadline of a command kind and derives the client
timeout from it.
"""

from __future__ import annotations

import time
from typing import Protocol

from .config import Settings, get_settings

#: Extra time the peer (agent or browser) may take on top of the server deadline.
CLIENT_TIMEOUT_GRACE_MS = 5_000


class Clock(Protocol):
    """Anything that can answer "what is the server time in epoch ms"."""

    def now_ms(self) -> int:
        """Return the current server time in epoch milliseconds."""


class SystemClock:
    """Production clock backed by ``time.time``."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)


SYSTEM_CLOCK = SystemClock()


def now_ms() -> int:
    """The server clock. Never use ``datetime.now()`` for persistence."""

    return SYSTEM_CLOCK.now_ms()


def server_deadline_ms(kind: str, settings: Settings | None = None) -> int:
    """Budget handed to an agent for *kind*: ``min(type cap, global ceiling)``."""

    cfg = settings or get_settings()
    return min(cfg.command_timeout_ms(kind), cfg.max_command_deadline_ms)


def deadline_ms(kind: str, settings: Settings | None = None) -> int:
    """Convenience alias used by DTO builders and tests."""

    return server_deadline_ms(kind, settings)


def client_timeout_s(kind: str, settings: Settings | None = None) -> float:
    """Client-side timeout for a command kind: ``deadline + 5 s`` (§5.4)."""

    return (server_deadline_ms(kind, settings) + CLIENT_TIMEOUT_GRACE_MS) / 1000.0


def deadline_remaining_ms(deadline_at_ms: int, *, clock: Clock | None = None) -> int:
    """Milliseconds left before an absolute deadline (0 when already passed)."""

    return max(0, int(deadline_at_ms) - (clock or SYSTEM_CLOCK).now_ms())


__all__ = [
    "CLIENT_TIMEOUT_GRACE_MS",
    "Clock",
    "SYSTEM_CLOCK",
    "SystemClock",
    "client_timeout_s",
    "deadline_ms",
    "deadline_remaining_ms",
    "now_ms",
    "server_deadline_ms",
]