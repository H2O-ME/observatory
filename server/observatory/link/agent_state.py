"""Persist agent reports into the ``devices`` row — one write path (C1).

Both protocol families must land the same facts in the same place:

* ``agent_version`` / ``boot_id`` / ``protocol`` / ``last_seen_ms`` are updated
  here, never in console DTOs or presence;
* a changed ``boot_id`` means the agent restarted, which is logged explicitly so
  the console can tell an old row from a live process;
* callers that already hold an ORM session use :func:`apply_agent_report`; the
  v3 link injects :func:`make_agent_report_reporter` so carriers stay thin.

Legacy 0.2.x heartbeat/hello should call the same :func:`apply_agent_report`
inside their existing transaction instead of hand-writing the fields.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..core.clock import now_ms
from ..core.log import log_event
from ..core.models import Device

log = logging.getLogger(__name__)

AgentReport = dict[str, Any]
#: ``(device_id, report_body, protocol) -> None`` — injected into AgentSession.
DeviceReporter = Callable[[int, AgentReport, int], Awaitable[None]]

_AGENT_VERSION_LEN = 32
_BOOT_ID_LEN = 64


def _clean(value: Any, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def apply_agent_report(
    device: Device,
    body: AgentReport,
    *,
    protocol: int | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Apply one hello/heartbeat report to *device*, returning changed fields."""

    moment = now if now is not None else now_ms()
    changed: dict[str, Any] = {}

    def update(attr: str, value: Any) -> None:
        if getattr(device, attr) != value:
            setattr(device, attr, value)
            changed[attr] = value

    if "agent_version" in body:
        update("agent_version", _clean(body.get("agent_version"), _AGENT_VERSION_LEN))
    if "boot_id" in body:
        previous = getattr(device, "boot_id", "") or ""
        boot_id = _clean(body.get("boot_id"), _BOOT_ID_LEN)
        update("boot_id", boot_id)
        if previous and boot_id and previous != boot_id:
            log_event(
                log,
                logging.INFO,
                "agent.rebooted",
                device_id=device.id,
                uid=device.uid,
                old_boot_id=previous,
                new_boot_id=boot_id,
            )
    if protocol is not None:
        update("protocol", int(protocol))
    _merge_sysinfo_report(device, body)
    update("last_seen_ms", moment)
    return changed


def _merge_sysinfo_report(device: Device, body: AgentReport) -> None:
    """Merge ``sysinfo`` and ``state.capture`` from hello/heartbeat into the row.

    0.3.0 dropped this for v3 (only the legacy adapter merged), which froze the
    dashboard and made capture-degraded capabilities unreachable for v3 devices
    (F3). Both protocols now land the same facts here.
    """

    sysinfo = body.get("sysinfo")
    if isinstance(sysinfo, dict) and sysinfo:
        device.sysinfo = {**(device.sysinfo or {}), **sysinfo}
    state = body.get("state")
    capture = state.get("capture") if isinstance(state, dict) else None
    if isinstance(capture, dict):
        device.sysinfo = {
            **(device.sysinfo or {}),
            "capture": {
                "state": str(capture.get("state") or "")[:30],
                "reason": str(capture.get("reason") or "")[:200],
                "workers": capture.get("workers") if isinstance(capture.get("workers"), list) else [],
                "sessions": int(capture.get("sessions") or 0),
                "at_ms": now_ms(),
            },
        }


async def record_agent_report(
    sessionmaker: Any,
    device_id: int,
    body: AgentReport,
    *,
    protocol: int | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Load the device, apply the report and commit in its own transaction."""

    async with sessionmaker() as session:
        device = await session.get(Device, int(device_id))
        if device is None:
            log_event(log, logging.WARNING, "agent.report_unknown_device", device_id=device_id)
            return {}
        changed = apply_agent_report(device, body, protocol=protocol, now=now)
        if changed:
            await session.commit()
    return changed


def make_agent_report_reporter(app: Any) -> DeviceReporter | None:
    """Build the injected reporter for v3 carriers; ``None`` without a database."""

    database = getattr(app.state, "db", None)
    if database is None:
        return None

    async def reporter(device_id: int, body: AgentReport, protocol: int) -> None:
        await record_agent_report(
            database.sessionmaker,
            device_id,
            body,
            protocol=protocol,
        )

    return reporter


__all__ = [
    "AgentReport",
    "DeviceReporter",
    "apply_agent_report",
    "make_agent_report_reporter",
    "record_agent_report",
]