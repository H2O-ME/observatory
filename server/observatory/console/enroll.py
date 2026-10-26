"""Device enrolment for the v3 link (``POST /api/v1/agent/enroll``).

v3-only since 0.3.1; the shared enrolment code is gone. Enrolment is gated by a
**deployment ticket** (``install_tokens``), which is deliberately a *distribution
gate*, not a per-device secret: it is long-lived and reusable across machines by
default, because its job is only to stop unauthorised parties from downloading
and enrolling the agent build.

Ordering (C6): the slot is consumed with a single conditional ``UPDATE`` inside
the same transaction as the device-row work, so a race loser leaves no device
row behind. ``install_token_uses`` makes binding idempotent per device: the same
machine re-enrolling (reinstall / 401 self-heal) never consumes another slot and
``max_uses`` counts machines, not registration attempts.

A device with a live session is still rejected as a takeover attempt and
audited. F9 resurrection: delete → reinstall is a normal ops cycle, so any valid
ticket (the admin's re-admission consent) pierces the delete tombstone.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.config import resolve_version
from ..core.crypto import device_token_hash, new_device_token
from ..core.db import get_session
from ..core.errors import AppError
from ..core.log import log_event
from ..core.models import DeletedDevice, Device, InstallToken, InstallTokenUse
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
    if row.revoked_at_ms is not None:
        raise AppError.auth("安装令牌已被撤销")
    if not row.permanent and int(row.expires_at_ms or 0) <= now_ms():
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
    # 取值必须早于任何 rollback：回滚会让 ORM 属性过期，之后再读会触发
    # 惰性加载并撞上已关闭的事务。
    token_limit = int(token_row.max_uses or 0)
    # F9: a deleted uid stays dead for old credentials; any valid ticket is the
    # admin's re-admission consent (delete → reinstall cycle).
    tombstone = (
        await session.execute(select(DeletedDevice).where(DeletedDevice.uid == uid))
    ).scalars().first()
    resurrected = tombstone is not None
    device = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()

    # --- takeover guard: refuse to rotate credentials of a live device -------
    # Runs before any counter change, so a rejected attempt never uses a slot.
    if device is not None and not device.uninstalled_at_ms and _live_session(request, device):
        await session.rollback()
        # 回滚会让 ORM 对象过期；审计只传 uid，避免回滚后再触发惰性加载。
        await audit.record(
            session,
            action=audit.ACTION_ENROLL,
            device_uid=uid,
            ip=ip,
            detail={"ok": False, "uid": uid, "reason": "device_online"},
        )
        log_event(log, logging.WARNING, "enroll.takeover_rejected", uid=uid, ip=ip)
        raise AppError.auth("设备在线中，拒绝重复注册；如需重装请先停止原受控端或先卸载")

    # --- binding slot: the same device never consumes twice ------------------
    already_bound = False
    if device is not None:
        already_bound = (
            await session.execute(
                select(InstallTokenUse.id).where(
                    InstallTokenUse.token_id == token_row.id,
                    InstallTokenUse.device_id == device.id,
                )
            )
        ).scalars().first() is not None
    if not already_bound and token_row.remaining_uses == 0:
        await session.rollback()
        await audit.record(
            session,
            action=audit.ACTION_ENROLL,
            device_uid=uid,
            ip=ip,
            detail={"ok": False, "uid": uid, "reason": "token_exhausted"},
        )
        raise AppError.auth(f"安装票据可绑定的设备台数已用完（上限 {token_limit} 台）")

    # --- device row: create or rotate, all in the slot's transaction ---------
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

    if not already_bound:
        bind = await session.execute(
            sqlite_insert(InstallTokenUse)
            .values(
                token_id=token_row.id,
                device_id=int(device.id),
                used_at_ms=now_ms(),
                ip=ip,
                hostname=str(payload.get("hostname") or "")[:128],
            )
            .on_conflict_do_nothing(index_elements=["token_id", "device_id"])
        )
        if bind.rowcount == 1:
            # 条件 UPDATE：并发注册到同一张限量票据时只有一个能扣到名额。
            charged = await session.execute(
                update(InstallToken)
                .where(
                    InstallToken.id == token_row.id,
                    InstallToken.revoked_at_ms.is_(None),
                    or_(InstallToken.max_uses == 0, InstallToken.used_count < InstallToken.max_uses),
                )
                .values(
                    used_count=InstallToken.used_count + 1,
                    used_at_ms=func.coalesce(InstallToken.used_at_ms, now_ms()),
                    used_device_id=int(device.id),
                    last_used_at_ms=now_ms(),
                )
            )
            if charged.rowcount != 1:
                await session.rollback()
                await audit.record(
                    session,
                    action=audit.ACTION_ENROLL,
                    ip=ip,
                    detail={"ok": False, "uid": uid, "reason": "token_exhausted"},
                )
                raise AppError.auth(f"安装票据可绑定的设备台数已用完（上限 {token_limit} 台）")
    if tombstone is not None:
        # Same transaction as the binding: a race loser never burns the tombstone,
        # and a resurrected uid always lands with a live row.
        await session.execute(delete(DeletedDevice).where(DeletedDevice.uid == uid))
    await session.commit()
    await session.refresh(device)
    await session.refresh(token_row)

    await audit.record(
        session,
        action=audit.ACTION_ENROLL,
        device=device,
        ip=ip,
        detail={
            "ok": True,
            "version": device.agent_version,
            "mode": "token",
            "reclaimed": already_bound,
            "bound_devices": int(token_row.used_count or 0),
            "resurrected": resurrected,
        },
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
