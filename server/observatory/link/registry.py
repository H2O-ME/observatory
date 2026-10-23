"""Device → live session registry — the only channel fact (C4).

Business code must never read this registry to decide whether a device is
online; that decision belongs to :mod:`observatory.presence`. Console code may
use it to obtain the session for sending a command.

Legacy (0.2.x) compatibility registers duck-typed sessions here, so both
protocols converge on one registry and one presence function.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..core.crypto import device_token_hash
from ..core.errors import AppError
from ..core.log import log_event

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """Authenticated device identity resolved before a session is created."""

    device_id: int
    uid: str
    token_hash: str = ""
    protocol: int = 3


@runtime_checkable
class DeviceDirectory(Protocol):
    """Resolve an ``X-Device-Token`` to a device identity."""

    async def authenticate(self, token: str) -> DeviceIdentity | None:
        """Return the identity for *token*, or ``None`` when it is invalid."""


class InMemoryDeviceDirectory:
    """W0 stand-in for the database-backed directory (W1 replaces this wiring).

    A non-empty token authenticates and deterministically maps to a device id.
    It exists only so the skeleton can receive a v3 envelope without a devices
    table lookup; it never weakens the v3 production path because ``main``
    logs a prominent warning while it is in use.
    """

    def __init__(self, secret_key: str) -> None:
        self._secret_key = secret_key

    async def authenticate(self, token: str) -> DeviceIdentity | None:
        cleaned = (token or "").strip()
        if not cleaned:
            return None
        digest = device_token_hash(cleaned, self._secret_key)
        device_id = int(digest[:12], 16)
        uid = f"OBS-{digest[:12].upper()}"
        return DeviceIdentity(device_id=device_id, uid=uid, token_hash=digest, protocol=3)


@runtime_checkable
class SessionLike(Protocol):
    """The frozen surface a registered session must expose (v3 or legacy)."""

    device_id: int
    uid: str
    carrier: str
    protocol: int
    last_seen_ms: int

    async def send(self, kind: str, body: dict[str, Any], *, deadline_ms: int) -> str:
        """Queue one downlink envelope and return its message id."""


class LinkRegistry:
    """One live session per device id; replacement closes the stale session."""

    def __init__(self) -> None:
        self._sessions: dict[int, Any] = {}
        self._lock = threading.Lock()

    def get(self, device_id: int) -> Any | None:
        with self._lock:
            return self._sessions.get(int(device_id))

    async def register(self, device_id: int, session: Any, *, replace: bool = True) -> Any | None:
        """Bind *session* to *device_id*. Returns the replaced session, if any."""

        key = int(device_id)
        with self._lock:
            previous = self._sessions.get(key)
            if previous is not None and previous is not session and not replace:
                return previous
            self._sessions[key] = session
        if previous is not None and previous is not session:
            log_event(
                log,
                logging.INFO,
                "registry.replaced",
                device_id=key,
                old_carrier=getattr(previous, "carrier", "?"),
                new_carrier=getattr(session, "carrier", "?"),
            )
            await _close_session(previous, code=4000, reason="replaced by newer connection")
        log_event(log, logging.DEBUG, "registry.registered", device_id=key, carrier=getattr(session, "carrier", "?"))
        return previous

    async def register_legacy(self, device_id: int, session: Any) -> Any | None:
        """Legacy 0.2.x entry point: a duck-typed session joins the same registry."""

        return await self.register(device_id, session)

    async def unregister(self, device_id: int, session: Any | None = None) -> bool:
        key = int(device_id)
        with self._lock:
            current = self._sessions.get(key)
            if current is None:
                return False
            if session is not None and current is not session:
                return False
            self._sessions.pop(key, None)
        log_event(log, logging.DEBUG, "registry.unregistered", device_id=key)
        return True

    def touch(self, device_id: int) -> None:
        session = self.get(device_id)
        if session is None:
            return
        touch = getattr(session, "touch", None)
        if callable(touch):
            touch()

    def sessions(self) -> list[Any]:
        with self._lock:
            return list(self._sessions.values())

    def counts(self) -> dict[str, int]:
        """Carrier counts for ``GET /api/v1/health``."""

        counts: dict[str, int] = {}
        for session in self.sessions():
            carrier = str(getattr(session, "carrier", "") or "none")
            counts[carrier] = counts.get(carrier, 0) + 1
        return counts

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    async def close_all(self, code: int = 1001, reason: str = "server shutdown") -> int:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await _close_session(session, code=code, reason=reason)
        return len(sessions)


async def _close_session(session: Any, *, code: int, reason: str) -> None:
    close = getattr(session, "close", None)
    if not callable(close):
        return
    try:
        await close(code=code, reason=reason)
    except Exception as exc:  # noqa: BLE001 - transport teardown must not mask shutdown
        # Observable: a failure to close one session must not abort the others.
        log_event(log, logging.WARNING, "registry.close_failed", error=str(exc))


#: Process-wide registry; also exposed as ``app.state.links`` by the assembly.
registry = LinkRegistry()


def require_session(device_id: int) -> Any:
    """Console helper: return the live session or raise a uniform GONE error."""

    session = registry.get(device_id)
    if session is None:
        raise AppError.gone("设备当前没有活动链路")
    return session


__all__ = [
    "DeviceDirectory",
    "DeviceIdentity",
    "InMemoryDeviceDirectory",
    "LinkRegistry",
    "SessionLike",
    "registry",
    "require_session",
]