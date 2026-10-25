"""Audit trail for the 0.3.0 console.

The audit row is written by the *caller's* transaction where the action itself
needs to be atomic (settings, device updates); security-sensitive rejected
attempts (failed login, refused command) get their own commit so they are still
recorded when the request raises afterwards.

Action names are dotted ``area.verb`` strings so the log stays greppable.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.db import get_session
from ..core.errors import AppError
from ..core.models import AuditEntry, Device
from .deps import client_ip, require_admin
from .dto import stamp
from .envelope import ConsoleRoute

log = logging.getLogger(__name__)

router = APIRouter(tags=["audit"], dependencies=[Depends(require_admin)], route_class=ConsoleRoute)

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
ACTION_CONTROL_VIEW = "control.view"
ACTION_FILE_OP = "files.op"
ACTION_PACKAGE_UPLOAD = "package.upload"
ACTION_PACKAGE_DELETE = "package.delete"
ACTION_ENROLL = "device.enroll"
ACTION_INSTALL_TOKEN_CREATE = "install.token_create"
ACTION_INSTALL_TOKEN_DELETE = "install.token_delete"
ACTION_BACKUP_RETRIEVE = "backup.retrieve"
ACTION_BACKUP_DELETE = "backup.delete"


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
    commit: bool = True,
) -> AuditEntry:
    """Append one audit entry.

    ``commit=False`` is for callers that are already inside a transaction which
    must stay atomic with the action; the default commits immediately so a
    rejected login survives the ``AppError`` raised right after it.
    """

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
    if commit:
        await session.commit()
    return entry


@router.get("/audit")
async def list_audit(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str = Query(default=""),
    device_uid: str = Query(default=""),
    actor: str = Query(default=""),
    ts_from_ms: int = Query(default=0, ge=0),
    ts_to_ms: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if ts_from_ms and ts_to_ms and ts_from_ms > ts_to_ms:
        raise AppError.bad_request("ts_from_ms 不能大于 ts_to_ms")
    statement = select(AuditEntry)
    counter = select(func.count(AuditEntry.id))
    if action:
        statement = statement.where(AuditEntry.action == action)
        counter = counter.where(AuditEntry.action == action)
    if device_uid:
        statement = statement.where(AuditEntry.device_uid == device_uid)
        counter = counter.where(AuditEntry.device_uid == device_uid)
    if actor:
        statement = statement.where(AuditEntry.actor == actor)
        counter = counter.where(AuditEntry.actor == actor)
    if ts_from_ms:
        statement = statement.where(AuditEntry.ts_ms >= ts_from_ms)
        counter = counter.where(AuditEntry.ts_ms >= ts_from_ms)
    if ts_to_ms:
        statement = statement.where(AuditEntry.ts_ms <= ts_to_ms)
        counter = counter.where(AuditEntry.ts_ms <= ts_to_ms)

    total = int((await session.execute(counter)).scalar() or 0)
    rows = list(
        (
            await session.execute(statement.order_by(AuditEntry.ts_ms.desc()).offset(offset).limit(limit))
        )
        .scalars()
        .all()
    )
    return stamp(
        {
            "entries": [
                {
                    "id": row.id,
                    "ts_ms": row.ts_ms,
                    "actor": row.actor,
                    "action": row.action,
                    "device_uid": row.device_uid,
                    "device_name": row.device_name,
                    "ip": row.ip,
                    "detail": row.detail or {},
                }
                for row in rows
            ],
            "total": total,
        }
    )


__all__ = [
    "ACTION_BACKUP_DELETE",
    "ACTION_BACKUP_RETRIEVE",
    "ACTION_COMMAND_CREATE",
    "ACTION_CONTROL_GRANT",
    "ACTION_DEVICE_DELETE",
    "ACTION_DEVICE_UNINSTALL",
    "ACTION_DEVICE_UPDATE",
    "ACTION_ENROLL",
    "ACTION_FILE_OP",
    "ACTION_INSTALL_TOKEN_CREATE",
    "ACTION_INSTALL_TOKEN_DELETE",
    "ACTION_LOGIN",
    "ACTION_LOGIN_FAILED",
    "ACTION_LOGOUT",
    "ACTION_PACKAGE_DELETE",
    "ACTION_PACKAGE_UPLOAD",
    "ACTION_PASSWORD_CHANGE",
    "ACTION_SETTINGS_UPDATE",
    "ACTION_TOTP_DISABLE",
    "ACTION_TOTP_ENABLE",
    "client_ip",
    "record",
    "router",
]