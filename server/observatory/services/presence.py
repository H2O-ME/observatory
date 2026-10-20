"""Presence: the single source of truth for "is this device reachable".

Every console surface (wall, device header, file manager, control page) calls
``presence.snapshot`` — there is no second opinion anywhere in the code base.
That is the structural fix for the old console showing *online* in the device
list while the file manager claimed the device was offline, or listing resource
usage for a device it also painted grey.

Two rules make the state stable instead of flickering:

1. A channel is only dropped when it is genuinely stale. A polling device keeps
   its ``poll`` channel alive across requests because each heartbeat refreshes
   ``last_ms``; nothing detaches at request end.
2. A state transition needs the full staleness window, not a single missed beat.
   ``WS_STALE_MS`` is several heartbeats wide.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ..models import Device, now_ms

#: A WebSocket channel with no traffic for this long is considered dead. The
#: agent sends a heartbeat every ``heartbeat_interval_s`` (default 20s), so this
#: tolerates two lost beats before the UI downgrades.
WS_STALE_MS = 65_000
#: A polling device refreshes its channel on every poll; the window has to cover
#: the slowest configured interval (120s) plus one round trip.
POLL_STALE_MS = 150_000
#: After this long without any contact the device is simply offline.
OFFLINE_AFTER_MS = 120_000

STATE_ONLINE = "online"
STATE_DEGRADED = "degraded"
STATE_OFFLINE = "offline"
STATE_UNINSTALLING = "uninstalling"
STATE_UNINSTALLED = "uninstalled"


@dataclass(slots=True)
class Channel:
    device_id: int
    kind: str  # "ws" | "poll"
    remote: str = ""
    since_ms: int = field(default_factory=now_ms)
    last_ms: int = field(default_factory=now_ms)

    def touch(self) -> None:
        self.last_ms = now_ms()

    def age_ms(self) -> int:
        return max(0, now_ms() - self.last_ms)


class ChannelRegistry:
    """In-process registry of live agent channels.

    One container serves the fleet, so an in-memory map is authoritative. If the
    server is ever scaled horizontally this becomes the only thing that needs to
    move (to Redis); the rest of the code only sees ``snapshot``.
    """

    def __init__(self) -> None:
        self._channels: dict[int, Channel] = {}
        self._lock = threading.Lock()

    def attach(self, device_id: int, kind: str, remote: str = "") -> Channel:
        with self._lock:
            existing = self._channels.get(device_id)
            if existing is not None and existing.kind == kind:
                existing.touch()
                if remote:
                    existing.remote = remote
                return existing
            # An HTTP heartbeat must never downgrade a live WebSocket: the agent
            # keeps reporting over both paths, and letting the poll request win
            # made the console claim "仅轮询，实时控制不可用" while the socket was
            # healthy — the exact kind of lying UI this rewrite exists to remove.
            if existing is not None and existing.kind == "ws" and kind != "ws":
                if existing.age_ms() <= WS_STALE_MS:
                    existing.touch()
                    if remote:
                        existing.remote = remote
                    return existing
            channel = Channel(device_id=device_id, kind=kind, remote=remote)
            # Upgrading poll -> ws keeps the original "seen since" so the UI does
            # not report a connection drop it never had.
            if existing is not None and kind == "ws":
                channel.since_ms = existing.since_ms
            self._channels[device_id] = channel
            return channel

    def touch(self, device_id: int) -> None:
        with self._lock:
            channel = self._channels.get(device_id)
            if channel is not None:
                channel.touch()

    def detach(self, device_id: int, kind: str | None = None) -> None:
        with self._lock:
            channel = self._channels.get(device_id)
            if channel is None:
                return
            if kind is None or channel.kind == kind:
                self._channels.pop(device_id, None)

    def get(self, device_id: int) -> Channel | None:
        with self._lock:
            return self._channels.get(device_id)

    def purge_stale(self) -> int:
        """Drop channels that have gone quiet; returns how many were removed."""

        removed = 0
        with self._lock:
            for device_id, channel in list(self._channels.items()):
                limit = WS_STALE_MS if channel.kind == "ws" else POLL_STALE_MS
                if channel.age_ms() > limit:
                    self._channels.pop(device_id, None)
                    removed += 1
        return removed

    def counts(self) -> dict[str, int]:
        with self._lock:
            result = {"ws": 0, "poll": 0}
            for channel in self._channels.values():
                result[channel.kind] = result.get(channel.kind, 0) + 1
            return result

    def clear(self) -> None:
        with self._lock:
            self._channels.clear()


def _capabilities(state: str) -> dict[str, bool]:
    """What genuinely works right now, given the state.

    ``files``/``screenshot``/``update`` travel over the command bus, which works
    on both channels (polling just adds latency). ``control`` needs a live
    WebSocket, so it is the only capability that a degraded device loses.
    """

    if state == STATE_ONLINE:
        return {
            "control": True,
            "files": True,
            "screenshot": True,
            "timeline": True,
            "update": True,
            "uninstall": True,
        }
    if state == STATE_DEGRADED:
        return {
            "control": False,
            "files": True,
            "screenshot": True,
            "timeline": True,
            "update": True,
            "uninstall": True,
        }
    return {
        "control": False,
        "files": False,
        "screenshot": False,
        "timeline": True,
        "update": False,
        "uninstall": False,
    }


def snapshot(
    device: Device,
    registry: ChannelRegistry,
    *,
    uninstall_pending: bool = False,
) -> dict:
    """Build the canonical presence payload for one device."""

    server_now = now_ms()
    channel = registry.get(device.id)

    if device.uninstalled_at_ms:
        state = STATE_UNINSTALLED
        transport = "none"
        reason = "受控端已卸载"
        last_seen = device.last_seen_ms or 0
        channel_since = None
    elif uninstall_pending:
        state = STATE_UNINSTALLING
        transport = channel.kind if channel else "none"
        reason = "正在执行远程卸载"
        last_seen = device.last_seen_ms or 0
        channel_since = channel.since_ms if channel else None
    else:
        fresh_ws = bool(channel and channel.kind == "ws" and channel.age_ms() <= WS_STALE_MS)
        fresh_poll = bool(channel and channel.kind == "poll" and channel.age_ms() <= POLL_STALE_MS)
        last_seen = max(device.last_seen_ms or 0, channel.last_ms if channel else 0)

        if fresh_ws:
            state = STATE_ONLINE
            transport = "ws"
            reason = "实时通道已连接"
        elif fresh_poll:
            state = STATE_DEGRADED
            transport = "poll"
            reason = "仅轮询通道，实时控制不可用"
        elif last_seen and server_now - last_seen <= OFFLINE_AFTER_MS:
            state = STATE_DEGRADED
            transport = "none"
            age = max(0, server_now - last_seen) // 1000
            reason = f"{age}s 前有上报，当前无活动通道"
        else:
            state = STATE_OFFLINE
            transport = "none"
            reason = "超过 2 分钟无任何上报"
        channel_since = channel.since_ms if channel else None

    age = max(0, server_now - last_seen) if last_seen else None
    return {
        "state": state,
        "transport": transport,
        "reason": reason,
        "last_seen_ms": last_seen,
        "age_ms": age,
        "channel_since_ms": channel_since,
        "capabilities": _capabilities(state),
    }


__all__ = [
    "Channel",
    "ChannelRegistry",
    "OFFLINE_AFTER_MS",
    "POLL_STALE_MS",
    "STATE_DEGRADED",
    "STATE_OFFLINE",
    "STATE_ONLINE",
    "STATE_UNINSTALLED",
    "STATE_UNINSTALLING",
    "WS_STALE_MS",
    "snapshot",
]