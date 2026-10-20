"""Append-only audit trail.

Written *before* the action is attempted and committed immediately, so a failed
or rejected action is still visible afterwards. The detail blob is deliberately
free-form: ops actions carry their parameters, file operations carry the path.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditEntry, Device, now_ms

# Action names are dotted ``area.verb`` strings so the log stays greppable.
ACTION_LOGIN = "auth.login"
ACTION_LOGIN_FAILED = "auth.login_failed"
ACTION_LOGOUT = "auth.logout"
ACTION_TOTP_ENABLE = "auth.totp_enable"
ACTION_TOTP_DISABLE = "auth.totp_disable"
ACTION_PASSWORD_CHANGE = "auth.password_change"
ACTION_SETTINGS_UPDATE = "settings.update"
ACTION_DEVICE_UPDATE = "device.update"
ACTION_DEVICE_DELETE = "device.delete"
ACTION_DEVICE_UNINSTALL = "device.uninstall"
ACTION_COMMAND_CREATE = "command.create"
ACTION_CONTROL_GRANT = "control.grant"
ACTION_FILE_OP = "files.op"
ACTION_PACKAGE_UPLOAD = "package.upload"
ACTION_PACKAGE_DELETE = "package.delete"
ACTION_ENROLL = "device.enroll"


async def record(
    session: AsyncSession,
    *,
    action: str,
    actor: str = "console",
    device: Device | None = None,
    device_uid: str | None = None,
    device_name: str | None = None,
    ip: str = "",
    detail: dict[str, Any] | None = None,
) -> AuditEntry:
    entry = AuditEntry(
        ts_ms=now_ms(),
        actor=actor,
        action=action,
        device_uid=(device.uid if device is not None else (device_uid or "")),
        device_name=(device.display_name if device is not None else (device_name or "")),
        ip=ip or "",
        detail=detail or {},
    )
    session.add(entry)
    await session.commit()
    return entry


__all__ = [name for name in dir() if name.startswith("ACTION_")] + ["record"]