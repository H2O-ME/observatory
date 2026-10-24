"""Device → live session registry — the only channel fact (C4).

Business code must never read this registry to decide whether a device is
online; that decision belongs to :mod:`observatory.presence`. Console code may
use it to obtain the session for sending a command.

Legacy (0.2.x) compatibility registers duck-typed sessions here, so both
protocols converge on one registry and one presence function.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.config import Settings
from ..core.crypto import device_token_hash
from ..core.errors import AppError
from ..core.log import log_event
from ..core.models import DeletedDevice, Device
from .session import AgentSession, SessionProtocol

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """Authenticated device identity resolved before a session is created."""

    device_id: int
    uid: str
    token_hash: str = ""
    protocol: int = 3
    agent_version: str = ""
    boot_id: str = ""
    settings: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class DeviceDirectory(Protocol):
    """Resolve an ``X-Device-Token`` to a device identity.

    Invalid/empty tokens return ``None`` (carriers map that to 401). A valid
    token for an uninstalled device raises ``AppError.gone`` so the carrier can
    answer 410 and the agent stops reconnecting instead of retrying forever.
    """

    async def authenticate(self, token: str) -> DeviceIdentity | None:
        """Return the identity for *token*, or ``None`` when it is invalid."""


class InMemoryDeviceDirectory:
    """**Test-only** deterministic directory (never wired in production).

    Production uses :class:`DatabaseDeviceDirectory`; this stand-in exists for
    unit tests that do not want to seed SQLite rows.
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


class DatabaseDeviceDirectory:
    """Authenticate device tokens against the ``devices`` table (W1 security).

    Token storage is the same HMAC used by console enrollment and legacy
    enrollment (``device_token_hash(token, settings.secret_key)``), so a token
    rotated during re-enrollment immediately invalidates the previous one.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessionmaker = sessionmaker
        self._secret_key = settings.secret_key

    async def authenticate(self, token: str) -> DeviceIdentity | None:
        cleaned = (token or "").strip()
        if not cleaned:
            return None
        digest = device_token_hash(cleaned, self._secret_key)
        async with self._sessionmaker() as session:
            device = (
                await session.execute(
                    select(Device).where(
                        Device.token_hash == digest,
                        Device.token_hash != "",
                    )
                )
            ).scalars().first()
            if device is None:
                # F9: a deleted device leaves a tombstone. Answer GONE (not 401)
                # so the agent treats this as terminal and stops retrying —
                # without it a deleted fleet member spins on 401→enroll forever.
                tombstone = (
                    await session.execute(
                        select(DeletedDevice).where(DeletedDevice.token_hash == digest)
                    )
                ).scalars().first()
            else:
                tombstone = None
        if tombstone is not None:
            raise AppError.gone("该设备记录已被删除")
        if device is None:
            return None
        if device.uninstalled_at_ms:
            raise AppError.gone("受控端已卸载，旧令牌不再可用")
        return DeviceIdentity(
            device_id=int(device.id),
            uid=str(device.uid or ""),
            token_hash=digest,
            protocol=int(device.protocol or 3),
            agent_version=str(device.agent_version or ""),
            boot_id=str(device.boot_id or ""),
            settings=dict(device.settings or {}),
        )


#: Backward-compatible alias — the actual contract is ``SessionProtocol``.
SessionLike = SessionProtocol


class LinkRegistry:
    """One live session per device id; replacement closes the stale session.

    Exactly one implementation family is allowed: :class:`AgentSession` (v3 and
    legacy adapters both subclass it). A duck-typed session is rejected loudly so
    C1 cannot silently regress into two state machines.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, SessionProtocol] = {}
        self._lock = threading.Lock()

    def get(self, device_id: int) -> SessionProtocol | None:
        with self._lock:
            return self._sessions.get(int(device_id))

    def has_fresh_session(self, device_id: int, *, within_ms: int) -> bool:
        """True when a live, recently-seen session exists for this device.

        The link layer owns "is this device talking to us right now" (C4);
        consumers outside link (e.g. the enrolment takeover guard) ask here
        instead of poking at session internals.
        """

        session = self.get(device_id)
        if session is None or bool(getattr(session, "closed", True)):
            return False
        from ..core.clock import now_ms  # noqa: PLC0415 - avoids import cycle at module load

        return now_ms() - int(getattr(session, "last_seen_ms", 0) or 0) <= int(within_ms)

    async def register(
        self,
        device_id: int,
        session: SessionProtocol,
        *,
        replace: bool = True,
    ) -> SessionProtocol | None:
        """Bind *session* to *device_id*. Returns the replaced session, if any."""

        if not isinstance(session, AgentSession):
            raise TypeError(
                "registry only accepts link.session.AgentSession; "
                f"got {type(session).__name__} (legacy adapters must subclass AgentSession)"
            )
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

    def register_if_absent(self, device_id: int, session: AgentSession) -> tuple[SessionProtocol, SessionProtocol | None]:
        """Atomically install *session* only when no equivalent one is live (F21).

        Returns ``(winner, loser)``. The HTTP carrier's "get→create→register"
        sequence raced between two concurrent POSTs: both saw ``None``, both
        registered, and ``register`` closed the winner's session out from under
        its long poll. Inside the registry lock this is compare-and-set.
        """

        if not isinstance(session, AgentSession):
            raise TypeError(
                "registry only accepts link.session.AgentSession; "
                f"got {type(session).__name__}"
            )
        key = int(device_id)
        with self._lock:
            current = self._sessions.get(key)
            if current is not None and not bool(getattr(current, "closed", True)) and int(
                getattr(current, "protocol", 0) or 0
            ) == int(getattr(session, "protocol", 0) or 0):
                return current, session
            self._sessions[key] = session
            return session, current

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
    "DatabaseDeviceDirectory",
    "DeviceDirectory",
    "DeviceIdentity",
    "InMemoryDeviceDirectory",
    "LinkRegistry",
    "SessionLike",
    "registry",
    "require_session",
]