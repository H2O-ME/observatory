"""Outbound DTO builders for the 0.3.0 console.

One rule, enforced here and nowhere else:

* every response that describes a device, a command, a file listing or a live
  session carries ``server_time_ms`` from the server clock (C5 / A12);
* every response that hands the caller a budget carries ``deadline_ms`` (A8);
* any time value that was measured on the controlled machine is named
  ``device_time_ms`` -- never ``mtime_ms`` / ``server_time_ms`` (C5);
* configuration sent to a browser or to an agent is passed through
  :func:`device_safe_config`, which strips ``SECRET_KEYS`` (A4).

The module deliberately returns plain dicts.  The old Pydantic response models
also lived in one place, but they ended up duplicating the field list in two
files; a dict built by one function is the smaller surface.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from .. import presence
from ..core.clock import now_ms, server_deadline_ms
from ..core.models import Device
from ..core.settings_spec import agent_view

__all__ = [
    "EDGE_EPOCH_MS",
    "budget_ms",
    "command_out",
    "device_out",
    "device_safe_config",
    "device_state",
    "file_entry_out",
    "presence_out",
    "stamp",
    "with_deadline",
]

#: Anything above this is an absolute epoch timestamp, not a duration.
#: ``core.clock.server_deadline_ms`` is specified as a budget in ms; the guard
#: keeps us honest if the implementation changes its return convention.
EDGE_EPOCH_MS = 10**12


def budget_ms(kind: str, settings: Any = None) -> int:
    """Return the server-side wait budget for ``kind``, as a duration in ms."""

    raw = server_deadline_ms(kind, settings) if settings is not None else server_deadline_ms(kind)
    value = int(raw)
    if value > EDGE_EPOCH_MS:
        value = max(0, value - now_ms())
    return max(0, value)


def stamp(payload: dict[str, Any], *, deadline: int | None = None) -> dict[str, Any]:
    """Attach the mandatory envelope fields to an outbound payload."""

    stamped = dict(payload)
    stamped["server_time_ms"] = now_ms()
    if deadline is not None:
        stamped["deadline_ms"] = int(deadline)
    return stamped


def with_deadline(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    """``stamp`` + the §5.4 budget for a response that triggers device work."""

    return stamp(payload, deadline=budget_ms(kind))


def device_safe_config(values: dict[str, Any] | None) -> dict[str, Any]:
    """Project configuration onto the agent-safe whitelist (A4).

    Run this for *all* outbound paths: agent config push, enrollment response,
    legacy heartbeat/poll and the console device payload.  The server-internal
    :meth:`SettingsStore.effective` is allowed to keep secrets; a raw dict must
    never be serialized to a browser or an agent.
    """

    return agent_view(values or {})


def _pick(obj: Any, *names: str) -> Any:
    """Read the first existing attribute from ``names``.

    The 0.3.0 command record keeps the same information as the old ``Command``
    row, but the journal's ``CommandRecord`` is free to use either ``id`` or
    ``command_id``.  Failing loudly here is better than serializing ``None``
    into the console and letting the UI guess.
    """

    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise AttributeError(f"object has none of the expected fields: {names!r}")


def command_out(command: Any) -> dict[str, Any]:
    """Serialize a ``Command`` ORM row or a journal ``CommandRecord``."""

    command_id = _pick(command, "id", "command_id")
    kind = _pick(command, "kind", "type")
    payload = _pick(command, "body", "payload")
    # ``commands.deadline_ms`` is written once by the journal (spec §6bis); the
    # budget helper is only a fallback for rows created before that column
    # existed, never the primary source.
    deadline = int(getattr(command, "deadline_ms", 0) or 0)
    if not deadline:
        deadline = budget_ms(str(kind))
    return {
        "id": str(command_id),
        "type": str(kind),
        "kind": str(kind),
        "status": str(_pick(command, "status")),
        "payload": dict(payload or {}),
        "result": _pick(command, "result"),
        "error": _pick(command, "error"),
        "error_code": _pick(command, "error_code"),
        "deadline_ms": deadline,
        "attempts": int(_pick(command, "attempts") or 0),
        "created_at_ms": int(_pick(command, "created_at_ms") or 0),
        "dispatched_at_ms": _pick(command, "dispatched_at_ms"),
        "finished_at_ms": _pick(command, "finished_at_ms"),
    }


def presence_out(state: dict[str, Any]) -> dict[str, Any]:
    """Presence is already a server-built dict; only stamp the envelope."""

    payload = dict(state)
    payload.setdefault("server_time_ms", now_ms())
    return payload


def device_state(
    device: Device,
    *,
    effective_settings: dict[str, Any],
    uninstall_pending: bool = False,
) -> Any:
    """Build the frozen ``presence.DeviceState`` for one ORM row.

    ``settings`` is deliberately the *effective* configuration: presence needs
    ``control_enabled`` to decide whether live control is possible (A13).
    Secrets are still fine here -- this object never leaves the server.
    """

    state = presence.DeviceState.from_orm(device)
    return replace(
        state,
        settings=dict(effective_settings),
        uninstall_pending=bool(uninstall_pending),
    )


def device_out(
    device: Device,
    *,
    state: dict[str, Any],
    effective_settings: dict[str, Any],
    latest_frame_id: int | None = None,
) -> dict[str, Any]:
    """One device row, with a device-safe settings view (A4)."""

    return {
        "uid": str(device.uid),
        "name": str(_pick(device, "display_name", "name", "hostname", "uid") or ""),
        "hostname": str(_pick(device, "hostname") or ""),
        "os_name": str(_pick(device, "os_name") or ""),
        "os_version": str(_pick(device, "os_version") or ""),
        "arch": str(_pick(device, "arch") or ""),
        "agent_version": str(_pick(device, "agent_version") or ""),
        "protocol": int(_pick(device, "protocol") or 0),
        "boot_id": _pick(device, "boot_id"),
        "enrolled_at_ms": int(_pick(device, "enrolled_at_ms") or 0),
        "last_seen_ms": int(_pick(device, "last_seen_ms") or 0),
        "uninstalled_at_ms": _pick(device, "uninstalled_at_ms"),
        "note": str(_pick(device, "note") or ""),
        "presence": presence_out(state),
        "sysinfo": dict(_pick(device, "sysinfo") or {}),
        "settings": device_safe_config(effective_settings),
        "latest_frame_id": latest_frame_id,
        "server_time_ms": now_ms(),
    }


def file_entry_out(raw: dict[str, Any]) -> dict[str, Any]:
    """Map one agent file entry to the console DTO.

    ``mtime_ms`` was the 0.2.x name for "the time reported by the controlled
    machine".  In 0.3.0 that value is named ``device_time_ms`` so the frontend
    can never mistake it for the server clock (C5 / A12).
    """

    device_time = raw.get("device_time_ms")
    if device_time is None:
        device_time = raw.get("mtime_ms")
    entry = {
        "name": str(raw.get("name", "")),
        "path": str(raw.get("path", "")),
        "is_dir": bool(raw.get("is_dir")),
        "size": int(raw.get("size") or 0),
        "device_time_ms": int(device_time or 0),
        "hidden": bool(raw.get("hidden")),
        "drive": str(raw.get("drive", "")),
        "free": int(raw.get("free") or 0),
        "filesystem": str(raw.get("filesystem", "")),
    }
    return entry


def _iter_commands(commands: Iterable[Any]) -> list[dict[str, Any]]:
    return [command_out(command) for command in commands]