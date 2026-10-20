"""Short-lived grants for the live-control WebSocket.

Control is the only capability that can move a mouse and type on someone's
machine, so it never inherits the console session implicitly: the operator takes
an explicit, audited grant with a TTL, and the grant is what the control socket
authenticates with.
"""

from __future__ import annotations

import asyncio
import secrets
import threading

from ..models import Device, now_ms

DEFAULT_TTL_S = 300
MAX_TTL_S = 3600
#: How many control frames may queue up for a browser that is slower than the
#: agent's capture rate. Old frames are dropped rather than letting latency grow.
MAX_QUEUED_FRAMES = 4


class ControlGrant:
    __slots__ = (
        "token",
        "device_id",
        "device_uid",
        "device_name",
        "fps",
        "quality",
        "expires_at_ms",
        "actor",
        "queue",
        "viewer_active",
    )

    def __init__(
        self,
        *,
        device: Device,
        fps: int,
        quality: int,
        ttl_s: int,
        actor: str,
    ) -> None:
        self.token = secrets.token_urlsafe(24)
        self.device_id = device.id
        self.device_uid = device.uid
        self.device_name = device.display_name
        self.fps = fps
        self.quality = quality
        self.expires_at_ms = now_ms() + max(30, min(MAX_TTL_S, ttl_s)) * 1000
        self.actor = actor
        self.queue: asyncio.Queue[bytes] | None = None
        self.viewer_active = False

    @property
    def expired(self) -> bool:
        return now_ms() >= self.expires_at_ms

    def as_dict(self) -> dict:
        return {
            "token": self.token,
            "expires_at_ms": self.expires_at_ms,
            "fps": self.fps,
            "quality": self.quality,
        }


class ControlSessions:
    def __init__(self) -> None:
        self._grants: dict[str, ControlGrant] = {}
        self._lock = threading.Lock()

    def grant(self, *, device: Device, fps: int, quality: int, ttl_s: int, actor: str) -> ControlGrant:
        grant = ControlGrant(device=device, fps=fps, quality=quality, ttl_s=ttl_s, actor=actor)
        with self._lock:
            self._purge_locked()
            self._grants[grant.token] = grant
        return grant

    def verify(self, token: str, *, device_id: int | None = None) -> ControlGrant | None:
        if not token:
            return None
        with self._lock:
            grant = self._grants.get(token)
            if grant is None:
                return None
            if grant.expired:
                self._grants.pop(token, None)
                return None
            if device_id is not None and grant.device_id != device_id:
                return None
            return grant

    def revoke(self, token: str) -> None:
        with self._lock:
            self._grants.pop(token, None)

    def revoke_device(self, device_id: int) -> int:
        with self._lock:
            tokens = [token for token, grant in self._grants.items() if grant.device_id == device_id]
            for token in tokens:
                self._grants.pop(token, None)
            return len(tokens)

    def active_count(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._grants)

    # -- viewer plumbing ---------------------------------------------------
    def attach_viewer(self, token: str) -> asyncio.Queue[bytes] | None:
        grant = self.verify(token)
        if grant is None:
            return None
        grant.queue = asyncio.Queue(maxsize=MAX_QUEUED_FRAMES)
        grant.viewer_active = True
        return grant.queue

    def detach_viewer(self, token: str) -> None:
        grant = self.verify(token)
        if grant is not None:
            grant.viewer_active = False
            grant.queue = None

    def push_frame(self, token: str, payload: bytes) -> bool:
        """Hand a control frame to the watching browser, dropping stale ones."""

        grant = self.verify(token)
        if grant is None or grant.queue is None:
            return False
        queue = grant.queue
        while queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            return False
        return True

    def _purge_locked(self) -> None:
        for token in [token for token, grant in self._grants.items() if grant.expired]:
            self._grants.pop(token, None)


__all__ = ["ControlGrant", "ControlSessions", "DEFAULT_TTL_S", "MAX_TTL_S"]