"""Device enrolment for the v3 link (``POST /api/v1/agent/enroll``).

P1 (0.4.0) makes enrolment **ticket-free and automatic**: a brand-new machine
registers itself on first start, an already-known device rotates its credential
by proving ownership, and a device whose record was removed simply registers
again. Nothing here can ban a device, and no ticket is consulted.

Order of decisions (each step short-circuits the next)
-----------------------------------------------------
1. per-IP bucket → 429 (sliding window, never a ban)
2. ``uid`` shape → 400
3. per-UID bucket + global bucket → 429
4. read the device row (read-only)
5. no row: optional ``OBS_ENROLL_KEY`` is checked **before any write**; on
   failure the response is 401 and the database is untouched (no row, no token,
   no history)
6. row says uninstalled: re-admission. A valid old token is proof; otherwise the
   same optional key gate applies, because the uninstall helper has deleted the
   local config so a genuine reinstall usually has no token left
7. row exists and the request proves the current token → CAS rotation
8. row exists and the request proves a *superseded* token still inside
   ``token_rotate_grace_s`` → CAS rotation (covers a lost enrolment response)
9. no proof, but a live session exists → 401 ``device_online`` (takeover guard)
10. no proof, no live session → 401 ``proof_required`` (someone knows a UID but
    owns nothing; this branch is the credential-less takeover signal)
11. concurrent insert on the same UID → rollback, re-read, re-decide (≤2 retries)

Two invariants this ordering exists to protect
----------------------------------------------
* **Credential before liveness.** Steps 6–8 run *before* the ``device_online``
  check. The guard exists to stop a third party taking over a live device; it
  must never stop the device itself from rotating its own credential. Normal
  agents poll every few seconds, so they are almost always "fresh" — checking
  liveness first would have broken every legitimate ``_reauth``.
* **Uninstalled before liveness.** A row marked uninstalled keeps no meaningful
  session; letting a stale one block re-admission would break the
  uninstall → reinstall cycle, which is a first-class lifecycle path.

``uid`` is an identifier only. It selects *which row* to update; it never decides
*whether the caller may*. Authentication is always ``X-Device-Token`` (current or
in-grace superseded), and that credential is what the rest of the system uses too
(:mod:`observatory.core.device_auth`).
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.config import resolve_version
from ..core.crypto import DEVICE_TOKEN_HEADER, device_token_hash, new_device_token
from ..core.db import get_session
from ..core.device_auth import MATCH_CURRENT, match_credential
from ..core.errors import AppError, ErrorCode
from ..core.log import log_event
from ..core.models import Device, DeviceTokenHistory
from . import audit
from .deps import client_ip, store
from .dto import device_safe_config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent-enroll"])

#: One creation attempt plus two re-decisions after a concurrent insert.
_ENROLL_ATTEMPTS = 3


class _RetryEnrolment(RuntimeError):
    """The row changed under us; the caller re-reads and re-decides."""


# ------------------------------------------------------------- rate limits ---
def _bucket(request: Request, name: str) -> Any | None:
    return getattr(request.app.state, name, None)


def _enforce_buckets(request: Request, *, uid: str, ip: str) -> None:
    """Three sliding-window buckets; expired hits simply fall out (no bans)."""

    for name, key in (
        ("enroll_ip_limiter", f"enroll:ip:{ip}"),
        ("enroll_uid_limiter", f"enroll:uid:{uid}"),
        ("enroll_global_limiter", "enroll:global"),
    ):
        limiter = _bucket(request, name)
        if limiter is not None and not limiter.allow(key):
            retry_after = limiter.retry_after(key)
            log_event(
                log,
                logging.WARNING,
                "enroll.rate_limited",
                bucket=name,
                uid=uid,
                ip=ip,
                retry_after=retry_after,
            )
            raise AppError.rate(
                f"注册过于频繁，请 {retry_after} 秒后再试", retry_after=retry_after
            )


# --------------------------------------------------------------- credentials ---
def _request_proof(request: Request, payload: dict[str, Any]) -> str:
    """The token the caller offers as proof of ownership (header wins)."""

    header = request.headers.get(DEVICE_TOKEN_HEADER, "").strip()
    if header:
        return header
    return str(payload.get("prev_token") or "").strip()


def _constant_time_equals(left: str, right: str) -> bool:
    """``hmac.compare_digest`` that tolerates non-ASCII operator keys."""

    try:
        return hmac.compare_digest(left, right)
    except TypeError:
        return left == right


def _require_admission_key(settings: Any, payload: dict[str, Any]) -> None:
    """Reject before any write when a deployment key is configured and missing.

    Called only for paths that **create/re-activate** a device record. Credential
    rotation of an existing, active device never requires it — otherwise rotating
    the key would knock a running fleet offline.
    """

    expected = str(getattr(settings, "enroll_key", "") or "")
    if not expected:
        return
    provided = str(payload.get("code") or payload.get("enroll_code") or "").strip()
    if provided and _constant_time_equals(provided, expected):
        return
    raise AppError.auth(
        "新设备准入密钥无效或缺失（enroll.key_required）；请使用控制台下发的安装脚本"
    )


def _live_session(request: Request, device: Device) -> bool:
    """True when a fresh link session exists for this device (takeover guard)."""

    links = getattr(request.app.state, "links", None)
    if links is None:
        return False
    return bool(
        links.has_fresh_session(
            device.id, within_ms=int(request.app.state.settings.poll_stale_ms)
        )
    )


# ------------------------------------------------------------------ helpers ---
def _clean(value: Any, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _apply_metadata(device: Device, payload: dict[str, Any]) -> None:
    """Copy the reported facts onto the row (never trusts lengths or types)."""

    device.hostname = _clean(payload.get("hostname"), 250)
    device.os_name = _clean(payload.get("os_name"), 60)
    device.os_version = _clean(payload.get("os_version"), 120)
    device.arch = _clean(payload.get("arch"), 30)
    device.agent_version = _clean(payload.get("agent_version"), 30)
    sysinfo = payload.get("sysinfo")
    device.sysinfo = sysinfo if isinstance(sysinfo, dict) else {}
    boot_id = str(payload.get("boot_id") or "")
    if boot_id:
        device.boot_id = boot_id[:64]
    if not device.name:
        device.name = _clean(payload.get("hostname") or device.uid, 120)


def _metadata_values(payload: dict[str, Any]) -> dict[str, Any]:
    """The same facts as a plain dict, for the conditional ``UPDATE``."""

    values: dict[str, Any] = {
        "hostname": _clean(payload.get("hostname"), 250),
        "os_name": _clean(payload.get("os_name"), 60),
        "os_version": _clean(payload.get("os_version"), 120),
        "arch": _clean(payload.get("arch"), 30),
        "agent_version": _clean(payload.get("agent_version"), 30),
    }
    sysinfo = payload.get("sysinfo")
    values["sysinfo"] = sysinfo if isinstance(sysinfo, dict) else {}
    boot_id = str(payload.get("boot_id") or "")
    if boot_id:
        values["boot_id"] = boot_id[:64]
    return values


async def _response(request: Request, session: AsyncSession, device: Device, token: str) -> dict[str, Any]:
    settings = request.app.state.settings
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


# ------------------------------------------------------------------- create ---
async def _create_device(
    request: Request,
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    uid: str,
    ip: str,
    now: int,
) -> dict[str, Any]:
    settings = request.app.state.settings
    token = new_device_token()
    device = Device(uid=uid, enrolled_at_ms=now)
    _apply_metadata(device, payload)
    device.token_hash = device_token_hash(token, settings.secret_key)
    device.uninstalled_at_ms = None
    device.last_seen_ms = now
    device.protocol = 3
    session.add(device)
    # flush, not commit: a concurrent INSERT on the same uid must surface as
    # IntegrityError so the caller can re-read the winner's row and re-decide.
    await session.flush()
    device_id = int(device.id)
    await session.commit()
    await session.refresh(device)
    await audit.record(
        session,
        action=audit.ACTION_ENROLL,
        device=device,
        ip=ip,
        detail={"ok": True, "mode": "first_seen", "device_id": device_id, "version": device.agent_version},
    )
    log_event(log, logging.INFO, "enroll.first_seen", uid=uid, device_id=device_id, ip=ip)
    return await _response(request, session, device, token)


# ------------------------------------------------------------------- rotate ---
async def _rotate_and_activate(
    request: Request,
    session: AsyncSession,
    device: Device,
    payload: dict[str, Any],
    *,
    ip: str,
    now: int,
    mode: str,
    clear_uninstalled: bool,
    matched: str,
) -> dict[str, Any]:
    """CAS-rotate the device token, then (optionally) clear the uninstall mark."""

    settings = request.app.state.settings
    device_id = int(device.id)
    previous = str(device.token_hash or "")
    token = new_device_token()

    values = _metadata_values(payload)
    values.update(
        {
            "token_hash": device_token_hash(token, settings.secret_key),
            "last_seen_ms": now,
            "protocol": 3,
        }
    )
    if clear_uninstalled:
        values["uninstalled_at_ms"] = None
        # P3: a reinstall must also cancel a queued cleanup, otherwise the next
        # housekeeping pass would delete this brand-new live record.
        values["purge_pending_at_ms"] = None
    if not device.name:
        values["name"] = _clean(payload.get("hostname") or device.uid, 120)

    # Compare-and-set: only the observed credential may be replaced. A racing
    # request that already rotated makes rowcount 0 and this attempt re-decides.
    outcome = await session.execute(
        update(Device)
        .where(Device.id == device_id, Device.token_hash == previous)
        .values(**values)
    )
    if outcome.rowcount != 1:
        await session.rollback()
        raise _RetryEnrolment(f"device {device_id} changed during enrolment")

    if previous:
        await session.execute(
            sqlite_insert(DeviceTokenHistory)
            .values(device_id=device_id, token_hash=previous, superseded_at_ms=now)
            .on_conflict_do_nothing(index_elements=["token_hash"])
        )
    await session.commit()
    await session.refresh(device)

    if clear_uninstalled:
        # A row marked uninstalled keeps no meaningful session; close it so the
        # reinstalled agent is not racing a ghost from the previous install.
        links = getattr(request.app.state, "links", None)
        if links is not None:
            try:
                await links.unregister(device_id)
            except Exception as exc:  # noqa: BLE001 - re-admission must not depend on it
                log_event(log, logging.WARNING, "enroll.unregister_failed", device_id=device_id, error=str(exc))

    detail: dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "device_id": device_id,
        "version": device.agent_version,
        "proof": matched or "none",
    }
    if clear_uninstalled:
        detail["readmit"] = "proof" if matched else "unverified"
    await audit.record(session, action=audit.ACTION_ENROLL, device=device, ip=ip, detail=detail)
    log_event(
        log,
        logging.INFO,
        "enroll.rotated",
        uid=device.uid,
        device_id=device_id,
        mode=mode,
        proof=matched or "none",
        ip=ip,
    )
    return await _response(request, session, device, token)


# ---------------------------------------------------------------- rejection ---
async def _reject(
    session: AsyncSession,
    *,
    uid: str,
    ip: str,
    reason: str,
    message: str,
) -> None:
    """Audit a refused enrolment, then raise. Writes no device state."""

    await session.rollback()
    await audit.record(
        session,
        action=audit.ACTION_ENROLL,
        device_uid=uid,
        ip=ip,
        detail={"ok": False, "uid": uid, "reason": reason},
    )
    log_event(log, logging.WARNING, f"enroll.{reason}", uid=uid, ip=ip)
    raise AppError.auth(message)


# -------------------------------------------------------------------- entry ---
@router.post("/enroll")
async def enroll(
    request: Request,
    payload: dict[str, Any] = Body(...),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    ip = client_ip(request)

    # 1 + 2 + 3: shape first (cheap), then the two UID-keyed buckets.
    limiter_ip = _bucket(request, "enroll_ip_limiter")
    if limiter_ip is not None and not limiter_ip.allow(f"enroll:ip:{ip}"):
        retry_after = limiter_ip.retry_after(f"enroll:ip:{ip}")
        raise AppError.rate(f"注册过于频繁，请 {retry_after} 秒后再试", retry_after=retry_after)

    uid = str(payload.get("uid") or "").strip()
    if not uid or len(uid) > 60:
        raise AppError.bad_request("设备标识不合法")

    _enforce_buckets(request, uid=uid, ip=ip)

    last_error: Exception | None = None
    for attempt in range(_ENROLL_ATTEMPTS):
        try:
            return await _enroll_once(request, session, payload, uid=uid, ip=ip)
        except (IntegrityError, _RetryEnrolment) as exc:
            last_error = exc
            await session.rollback()
            log_event(
                log,
                logging.WARNING,
                "enroll.concurrent_retry",
                uid=uid,
                ip=ip,
                attempt=attempt + 1,
                error=str(exc)[:200],
            )
            continue

    log_event(log, logging.ERROR, "enroll.concurrent_exhausted", uid=uid, ip=ip, error=str(last_error)[:200])
    raise AppError(
        ErrorCode.BAD_REQUEST,
        "enroll.concurrent：同一设备同时注册，请稍后重试",
        http_status=409,
        retryable=True,
    )


async def _enroll_once(
    request: Request,
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    uid: str,
    ip: str,
) -> dict[str, Any]:
    settings = request.app.state.settings
    now = now_ms()
    proof = _request_proof(request, payload)
    device = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()

    # 5. brand-new device: the optional deployment key gates creation, and it is
    #    checked *before* the row is inserted, so a failure leaves no residue.
    if device is None:
        _require_admission_key(settings, payload)
        return await _create_device(request, session, payload, uid=uid, ip=ip, now=now)

    matched = await match_credential(session, device, proof, settings=settings)

    # 6. uninstalled row: re-admission. Proof wins; otherwise the admission gate
    #    (not the mere presence of a UID) is what authorises clearing the mark.
    if device.uninstalled_at_ms:
        if not matched:
            _require_admission_key(settings, payload)
        return await _rotate_and_activate(
            request,
            session,
            device,
            payload,
            ip=ip,
            now=now,
            mode="readmit_proof" if matched else "readmit_unverified",
            clear_uninstalled=True,
            matched=matched,
        )

    # 7 + 8. an active device proving ownership may always rotate/recover.
    if matched:
        return await _rotate_and_activate(
            request,
            session,
            device,
            payload,
            ip=ip,
            now=now,
            mode="rotate" if matched == MATCH_CURRENT else "rotate_grace",
            clear_uninstalled=False,
            matched=matched,
        )

    # 9 + 10. no proof: only a third party reaches this. Distinguish the two
    # reasons so operators can tell a takeover attempt from a stale UID guess.
    online = _live_session(request, device)
    if online:
        await _reject(
            session,
            uid=uid,
            ip=ip,
            reason="device_online",
            message="设备在线中，拒绝接管（enroll.device_online）；如需重装请先远程卸载",
        )
    await _reject(
        session,
        uid=uid,
        ip=ip,
        reason="proof_required",
        message=(
            "该设备记录已存在，注册需要持有原设备令牌（enroll.proof_required）；"
            "若该机器已重装，请先在控制台删除这条设备记录"
        ),
    )
    raise AppError.internal("unreachable")  # pragma: no cover - _reject always raises


__all__ = ["router"]
