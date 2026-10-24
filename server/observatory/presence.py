"""Presence and capabilities — the **only** online decision (C4, A13).

Every consumer (console, legacy, media, health) must call :func:`snapshot`; no
other module may interpret channel freshness or build a capability map. All
thresholds come from :class:`~observatory.core.config.Settings`; there is no
hardcoded 65 s/120 s anymore.

The module is configured once by the application assembly::

    presence.configure(settings, registry, latest_agent_version=...)

``snapshot`` then works without passing settings around, while
:class:`PresenceContext` is available for tests that want an isolated instance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from .core.clock import Clock, SYSTEM_CLOCK
from .core.config import Settings, can_self_update, is_newer
from .core.log import log_event
from .link.registry import LinkRegistry

log = logging.getLogger(__name__)

STATE_ONLINE = "online"
STATE_DEGRADED = "degraded"
STATE_OFFLINE = "offline"
STATE_UNINSTALLING = "uninstalling"
STATE_UNINSTALLED = "uninstalled"

TRANSPORT_WS = "ws"
TRANSPORT_POLL = "poll"
TRANSPORT_NONE = "none"

_CAPTURE_BLOCKING_STATES = frozenset({"blocked", "no_session"})


@dataclass(frozen=True, slots=True)
class DeviceState:
    """Everything presence needs to know about a device (no ORM dependency)."""

    device_id: int
    uid: str
    last_seen_ms: int = 0
    agent_version: str = ""
    protocol: int = 2
    uninstalled_at_ms: int | None = None
    uninstall_pending: bool = False
    settings: Mapping[str, Any] = field(default_factory=dict)
    capture: Mapping[str, Any] | None = None
    boot_id: str = ""

    @classmethod
    def from_orm(cls, device: Any) -> DeviceState:
        """Map an ORM ``Device`` (or any duck-typed row) into the presence input."""

        sysinfo = getattr(device, "sysinfo", None)
        capture = sysinfo.get("capture") if isinstance(sysinfo, dict) else None
        return cls(
            device_id=int(getattr(device, "id", 0) or 0),
            uid=str(getattr(device, "uid", "") or ""),
            last_seen_ms=int(getattr(device, "last_seen_ms", 0) or 0),
            agent_version=str(getattr(device, "agent_version", "") or ""),
            protocol=int(getattr(device, "protocol", 2) or 2),
            uninstalled_at_ms=getattr(device, "uninstalled_at_ms", None),
            settings=dict(getattr(device, "settings", None) or {}),
            capture=capture if isinstance(capture, Mapping) else None,
            boot_id=str(getattr(device, "boot_id", "") or ""),
        )


def _transport_of(carrier: str | None) -> str:
    if carrier == TRANSPORT_WS:
        return TRANSPORT_WS
    if carrier in {"http", "poll"}:
        return TRANSPORT_POLL
    return TRANSPORT_NONE


def _effective_capture(capture: Mapping[str, Any] | None, settings: Settings, now: int) -> dict[str, Any] | None:
    if not isinstance(capture, Mapping):
        return None
    reported = int(capture.get("at_ms") or 0)
    # Missing/zero at_ms is stale by definition: an untimestamped health report
    # must never keep capabilities alive forever.
    if not reported or now - reported > settings.capture_stale_ms:
        return None
    return {
        "state": str(capture.get("state") or ""),
        "reason": str(capture.get("reason") or ""),
        "sessions": capture.get("sessions"),
        "workers": list(capture.get("workers") or []),
        "at_ms": reported,
    }


@dataclass
class PresenceContext:
    """Isolated evaluator; production uses the module-level configured instance."""

    settings: Settings
    registry: LinkRegistry
    clock: Clock = SYSTEM_CLOCK
    latest_agent_version: str | None = None

    def snapshot(self, device: DeviceState | Any) -> dict[str, Any]:
        state = device if isinstance(device, DeviceState) else DeviceState.from_orm(device)
        now = self.clock.now_ms()

        session = None if state.uninstalled_at_ms else self.registry.get(state.device_id)
        carrier = str(getattr(session, "carrier", "") or "") if session is not None else ""
        session_last = int(getattr(session, "last_seen_ms", 0) or 0) if session is not None else 0
        channel_since = getattr(session, "connected_at_ms", None) if session is not None else None
        protocol = int(getattr(session, "protocol", 0) or state.protocol) if session is not None else state.protocol
        last_seen = max(int(state.last_seen_ms or 0), session_last)

        if state.uninstalled_at_ms:
            presence_state = STATE_UNINSTALLED
            transport = TRANSPORT_NONE
            reason = "受控端已卸载"
        elif state.uninstall_pending:
            presence_state = STATE_UNINSTALLING
            transport = _transport_of(carrier)
            reason = "正在执行远程卸载"
        else:
            fresh_ws = carrier == TRANSPORT_WS and session_last > 0 and now - session_last <= self.settings.ws_stale_ms
            fresh_poll = carrier in {"http", "poll"} and session_last > 0 and now - session_last <= self.settings.poll_stale_ms
            if fresh_ws:
                presence_state = STATE_ONLINE
                transport = TRANSPORT_WS
                reason = "实时链路已连接"
            elif fresh_poll:
                presence_state = STATE_ONLINE
                transport = TRANSPORT_POLL
                reason = "轮询链路已连接"
            elif last_seen and now - last_seen <= self.settings.offline_after_ms:
                presence_state = STATE_DEGRADED
                transport = TRANSPORT_NONE
                reason = f"{max(0, now - last_seen) // 1000} 秒前有上报，当前没有活动链路"
            else:
                presence_state = STATE_OFFLINE
                transport = TRANSPORT_NONE
                reason = f"超过 {self.settings.agent_offline_after_s} 秒没有任何上报"

        capture = _effective_capture(state.capture, self.settings, now)
        has_newer_package = self._has_newer_package(state)
        capabilities = _capabilities(presence_state, transport, state, capture, has_newer_package)
        if presence_state in {STATE_ONLINE, STATE_DEGRADED} and capture:
            capture_state = str(capture.get("state") or "")
            if capture_state in _CAPTURE_BLOCKING_STATES:
                detail = str(capture.get("reason") or "")
                if capture_state == "no_session":
                    reason = detail or "没有已登录的交互式桌面，无法采集屏幕"
                else:
                    reason = f"采屏进程未运行：{detail}" if detail else "采屏进程未运行"
                capabilities = {**capabilities, "screenshot": False, "control": False}

        age_ms = max(0, now - last_seen) if last_seen else None
        return {
            "state": presence_state,
            "transport": transport,
            "reason": reason,
            "last_seen_ms": last_seen,
            "age_ms": age_ms,
            "channel_since_ms": channel_since,
            "capabilities": capabilities,
            "capture": capture,
            "self_update_blocked": self._self_update_blocked(state),
            "protocol": protocol,
            "server_time_ms": now,
        }

    def _self_update_blocked(self, state: DeviceState) -> str | None:
        version = (state.agent_version or "").strip()
        if can_self_update(version):
            return None
        if not version:
            return "受控端版本未知，无法自动升级，需重新运行安装脚本"
        return f"受控端 {version} 无法自动升级，需重新运行安装脚本"

    def _has_newer_package(self, state: DeviceState) -> bool:
        """A newer agent package exists and the device is on an upgradeable base."""

        latest = (self.latest_agent_version or "").strip()
        if not latest:
            return False
        return is_newer(latest, state.agent_version or "")


def _capabilities(
    state: str,
    transport: str,
    device: DeviceState,
    capture: Mapping[str, Any] | None,
    has_newer_package: bool,
) -> dict[str, bool]:
    """The single capability derivation (A13). Nothing else may compute this."""

    active = state == STATE_ONLINE
    capture_state = str((capture or {}).get("state") or "")
    capture_ok = active and capture_state not in _CAPTURE_BLOCKING_STATES
    control_enabled = bool(device.settings.get("control_enabled", True))
    return {
        "control": active and transport == TRANSPORT_WS and control_enabled and capture_ok,
        "files": active,
        "screenshot": capture_ok,
        "timeline": True,
        "update": active and has_newer_package and can_self_update(device.agent_version),
        "uninstall": active,
    }


_context: PresenceContext | None = None


def configure(
    settings: Settings,
    registry: LinkRegistry,
    *,
    latest_agent_version: str | None = None,
    clock: Clock | None = None,
) -> PresenceContext:
    """Bind the process-wide evaluator (idempotent; called by ``main.lifespan``)."""

    global _context
    _context = PresenceContext(
        settings=settings,
        registry=registry,
        clock=clock or SYSTEM_CLOCK,
        latest_agent_version=latest_agent_version,
    )
    log_event(
        log,
        logging.INFO,
        "presence.configured",
        ws_stale_ms=settings.ws_stale_ms,
        poll_stale_ms=settings.poll_stale_ms,
        offline_after_ms=settings.offline_after_ms,
        capture_stale_ms=settings.capture_stale_ms,
    )
    return _context


def set_latest_agent_version(version: str | None) -> None:
    """Refresh the package-fed part of the update capability (console uploads)."""

    context = _require_context()
    context.latest_agent_version = version
    log_event(log, logging.INFO, "presence.latest_agent_version", version=version)


def reset() -> None:
    global _context
    _context = None


def _require_context() -> PresenceContext:
    if _context is None:
        raise RuntimeError("presence not configured; call presence.configure() during startup")
    return _context


def snapshot(device: DeviceState | Any) -> dict[str, Any]:
    """The single online/capabilities judgement used by every consumer."""

    return _require_context().snapshot(device)


def snapshot_many(devices: list[DeviceState]) -> list[dict[str, Any]]:
    context = _require_context()
    return [context.snapshot(device) for device in devices]


__all__ = [
    "STATE_DEGRADED",
    "STATE_OFFLINE",
    "STATE_ONLINE",
    "STATE_UNINSTALLED",
    "STATE_UNINSTALLING",
    "TRANSPORT_NONE",
    "TRANSPORT_POLL",
    "TRANSPORT_WS",
    "DeviceState",
    "PresenceContext",
    "configure",
    "reset",
    "set_latest_agent_version",
    "snapshot",
    "snapshot_many",
]