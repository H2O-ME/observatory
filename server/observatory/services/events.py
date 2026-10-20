"""Typed console events.

One place that builds the envelopes pushed to ``/api/v1/ws`` so the WebUI can
switch on a closed set of names. The old console listened for ``command`` while
the server emitted ``command_result``, which is why the wall never refreshed when
a command finished.
"""

from __future__ import annotations

from typing import Any

from ..models import now_ms

EVENT_DEVICE_UPDATED = "device_updated"
EVENT_DEVICE_REMOVED = "device_removed"
EVENT_COMMAND_CREATED = "command_created"
EVENT_COMMAND_UPDATED = "command_updated"
EVENT_FRAME_ADDED = "frame_added"
EVENT_UNINSTALL_PROGRESS = "uninstall_progress"
EVENT_CONTROL_FRAME = "control_frame"
EVENT_VERSION = "version"
EVENT_NOTICE = "notice"

ALL_EVENTS = frozenset(
    {
        EVENT_DEVICE_UPDATED,
        EVENT_DEVICE_REMOVED,
        EVENT_COMMAND_CREATED,
        EVENT_COMMAND_UPDATED,
        EVENT_FRAME_ADDED,
        EVENT_UNINSTALL_PROGRESS,
        EVENT_CONTROL_FRAME,
        EVENT_VERSION,
        EVENT_NOTICE,
    }
)


def build(event_type: str, *, device_uid: str = "", data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": event_type,
        "ts_ms": now_ms(),
        "device_uid": device_uid,
        "data": data or {},
    }


async def emit(console_hub: Any, event_type: str, *, device_uid: str = "", data: dict[str, Any] | None = None) -> None:
    if console_hub is None:
        return
    await console_hub.broadcast(build(event_type, device_uid=device_uid, data=data))


__all__ = [
    "ALL_EVENTS",
    "EVENT_COMMAND_CREATED",
    "EVENT_COMMAND_UPDATED",
    "EVENT_CONTROL_FRAME",
    "EVENT_DEVICE_REMOVED",
    "EVENT_DEVICE_UPDATED",
    "EVENT_FRAME_ADDED",
    "EVENT_NOTICE",
    "EVENT_UNINSTALL_PROGRESS",
    "EVENT_VERSION",
    "build",
    "emit",
]