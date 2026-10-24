"""Device enrolment for the v3 link (``POST /api/v1/agent/enroll``).

0.3.1 (F0): the 0.2.x translator is gone together with the shared enrolment
code; enrolment is v3-only and redeemable only with a one-time install token
(A18). The path stays ``/api/v1/agent/enroll`` because every agent since 0.3.0
points here.

Ordering (C6): the token is claimed with a single conditional ``UPDATE``
**inside the same transaction** as the device-row work, so the loser of a
token race leaves no device row behind and rotates nothing. A token already
bound to *this* device may be re-redeemed (A17 self-heal after e.g. a database
restore). An enrolment for a uid with a live session is rejected as a takeover
attempt and audited.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.config import resolve_version
from ..core.crypto import device_token_hash, new_device_token
from ..core.db import get_session
from ..core.errors import AppError
from ..core.log import log_event
from ..core.models import DeletedDevice, Device, InstallToken
from . import audit
from .deps import client_ip, store
from .dto import device_safe_config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent-enroll"])

_ENROLL_LIMITER_KEY = "enroll_limiter"


def _limiter(request: Request) -> Any | None:
    return getattr(request.app.state, _ENROLL_LIMITER_KEY, None)


async def _load_token_row(
    session: AsyncSession, install_token: str, *, settings: Any
) -> InstallToken:
    """Select the token row by HMAC; never a plaintext lookup."""

    digest = device_token_hash(install_token, settings.secret_key)
    row = (
        await session.execute(select(InstallToken).where(InstallToken.token_hash == digest))
    ).scalars().first()
    if row is None:
        raise AppError.auth("安装令牌无效")
    if int(row.expires_at_ms or 0) <= now_ms():
        raise AppError.auth("安装令牌已过期")
    return row


def _live_session(request: Request, device: Device) -> bool:
    """True when a fresh link session exists for this device (takeover guard).

    The freshness judgment lives on the registry (C4: link owns online facts);
    enrolment only asks.
    """

    links = getattr(request.app.state, "links", None)
    if links is None:
        return False
    return bool(
        links.has_fresh_session(device.id, within_ms=int(request.app.state.settings.poll_stale_ms))
    )


@router.post("/enroll")
async def enroll(
    request: Request,
    payload: dict[str, Any] = Body(...),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    settings = request.app.state.settings
    ip = client_ip(request)
    limiter = _limiter(request)
    if limiter is not None and not limiter.allow(f"enroll:{ip}"):
        raise AppError.rate(f"注册过于频繁，请 {limiter.retry_after(f'enroll:{ip}')} 秒后再试")

    uid = str(payload.get("uid") or "").strip()
    if not uid or len(uid) > 60:
        raise AppError.bad_request("设备标识不合法")
    install_token = str(payload.get("install_token") or payload.get("token") or "").strip()
    if not install_token:
        raise AppError.auth("缺少安装令牌")

    token_row = await _load_token_row(session, install_token, settings=settings)
    # F9: a deleted uid stays deleted — do not resurrect it under a new token.
    tombstone = (
        await session.execute(select(DeletedDevice).where(DeletedDevice.uid == uid))
    ).scalars().first()
    if tombstone is not None:
        await audit.record(
            session,
            action=audit.ACTION_ENROLL,
            ip=ip,
            detail={"ok": False, "uid": uid, "reason": "tombstoned"},
        )
        raise AppError.auth("该设备记录已被管理员删除；如需重新接入请先清除墓碑记录")
    device = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()

    # --- claim the token (or verify the same-device re-redeem) ---------------
    if token_row.used_at_ms is not None:
        bound_to = token_row.used_device_id
        if device is None or bound_to is None or int(bound_to) != int(device.id):
            await session.rollback()
            await audit.record(
                session,
                action=audit.ACTION_ENROLL,
                ip=ip,
                detail={"ok": False, "uid": uid, "reason": "token_used"},
            )
            raise AppError.auth("安装令牌已被使用")
        claimed = False
    else:
        result = await session.execute(
            update(InstallToken)
            .where(InstallToken.id == token_row.id, InstallToken.used_at_ms.is_(None))
            .values(used_at_ms=now_ms())
        )
        if result.rowcount != 1:
            await session.rollback()
            raise AppError.auth("安装令牌已被使用")
        claimed = True

    # --- takeover guard: refuse to rotate credentials of a live device -------
    if device is not None and not device.uninstalled_at_ms and _live_session(request, device):
        await session.rollback()
        await audit.record(
            session,
            action=audit.ACTION_ENROLL,
            device=device,
            ip=ip,
            detail={"ok": False, "uid": uid, "reason": "device_online"},
        )
        log_event(log, logging.WARNING, "enroll.takeover_rejected", uid=uid, ip=ip)
        raise AppError.auth("设备在线中，拒绝重复注册；如需重装请先停止原受控端或先卸载")

    # --- device row: create or rotate, all in the claim's transaction --------
    token = new_device_token()
    digest = device_token_hash(token, settings.secret_key)
    if device is None:
        device = Device(uid=uid, enrolled_at_ms=now_ms())
        session.add(device)
    device.hostname = str(payload.get("hostname") or "")[:250]
    device.os_name = str(payload.get("os_name") or "")[:60]
    device.os_version = str(payload.get("os_version") or "")[:120]
    device.arch = str(payload.get("arch") or "")[:30]
    device.agent_version = str(payload.get("agent_version") or "")[:30]
    device.sysinfo = payload.get("sysinfo") if isinstance(payload.get("sysinfo"), dict) else {}
    device.token_hash = digest
    device.uninstalled_at_ms = None
    device.last_seen_ms = now_ms()
    device.protocol = 3
    boot_id = str(payload.get("boot_id") or "")
    if boot_id:
        device.boot_id = boot_id[:64]
    if not device.name:
        device.name = str(payload.get("hostname") or uid)[:120]
    await session.flush()
    if claimed:
        token_row.used_device_id = int(device.id)
    await session.commit()
    await session.refresh(device)

    await audit.record(
        session,
        action=audit.ACTION_ENROLL,
        device=device,
        ip=ip,
        detail={"ok": True, "version": device.agent_version, "mode": "token", "reclaimed": not claimed},
    )
    effective = await store().effective(session, device.settings or {})
    return {
        "device_uid": device.uid,
        "token": token,
        "config": device_safe_config(effective),
        "base_urls": list(settings.base_urls),
        "server_version": resolve_version(),
        "server_time_ms": now_ms(),
        "heartbeat_interval_s": int(effective.get("heartbeat_interval_s", 20)),
    }


__all__ = ["router"]
