"""Removing a device's activity record, with retry (P3/S5).

Two callers need the same sequence: the explicit console delete, and the
confirmed-uninstall path that now removes the record automatically. Keeping the
order in one place matters because each step has a different failure mode:

1. drop the live session (in-memory, cannot fail meaningfully),
2. revoke control grants (idempotent),
3. delete media — local tree, remote archive, frame rows (can fail: disk busy,
   archive unreachable),
4. delete the ``devices`` row (cascades frames/token history; ``commands``
   survive with ``device_id = NULL`` thanks to the v8 migration),
5. audit + event.

Only step 3 is expected to fail. When it does, the device row is **kept** and
flagged with ``purge_pending_at_ms`` *plus the intent* (``purge_reason`` /
``purge_remote``, v10) so housekeeping can retry the same request: reporting a
clean delete while media remains would be exactly the dishonest state C6 forbids.

The flag is not a ban — the row disappears as soon as a retry succeeds. A
reinstall cancels the queued task (``enrol._rotate_and_activate`` clears all three
columns on **every** successful rotation, not only on re-admission), so a stale
flag can never delete a machine that came back. The narrow race of a rotation
landing *inside* this pass is covered twice: the row is re-read by column (an
identity-map cached object would hide the change) before step 3, and the row
itself is removed with a compare-and-set on ``(token_hash, purge_pending_at_ms)``,
so the new install's record survives even if the rotation happened during the
media step.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.models import Device
from . import audit

log = logging.getLogger(__name__)

#: Why the record is queued for cleanup. Persisted so a retry does the *same*
#: thing the operator originally asked for.
PURGE_REASON_MANUAL = "manual"
PURGE_REASON_AFTER_UNINSTALL = "auto_after_uninstall"


async def purge_device_record(
    app: Any,
    session: AsyncSession,
    device: Device,
    *,
    reason: str,
    purge_remote: bool = True,
    ip: str = "",
) -> dict[str, Any]:
    """Delete one device's record and data; returns a machine-readable summary.

    ``ok=False`` means the media step failed: the row is still there and
    ``purge_pending_at_ms``/``purge_reason`` are set for the retry pass.
    """

    device_id = int(device.id)
    uid = str(device.uid or "")
    #: Incarnation witness: a reinstall rotates ``token_hash``, which is how this
    #: call tells "the same device record" from "the same row, now a new install".
    seen_token = str(device.token_hash or "")
    seen_pending = device.purge_pending_at_ms

    # 先判定再产生副作用：给一台刚重装回来的设备"踢会话 + 撤授权"同样是误伤。
    if not await _row_unchanged(session, device_id, seen_token, seen_pending):
        log.info("device purge abandoned for %s: record changed before cleanup", uid)
        return _stale_result(uid, device_id, "设备记录已被重新注册，排队中的清理任务作废")

    links = getattr(app.state, "links", None)
    if links is not None:
        await links.unregister(device_id)

    try:
        from .control import revoke_device_grants

        await revoke_device_grants(app, device_id, reason=f"device {reason}")
    except Exception as exc:  # noqa: BLE001 - grants are in-memory bookkeeping
        log.warning("revoke grants failed for %s: %s", uid, exc)

    if not await _row_unchanged(session, device_id, seen_token, seen_pending):
        # 设备行在会话/授权清理期间被重新注册：这不是"删除失败"，而是"任务已过期"。
        # 绝不能继续删这台机器现在的画面数据。
        log.info("device purge abandoned for %s: record changed (reinstalled?)", uid)
        return _stale_result(uid, device_id, "设备记录已被重新注册，排队中的清理任务作废")

    try:
        from ..media import ingest as media_ingest
        from .deps import store
    except ImportError as exc:  # pragma: no cover - media layer optional at import time
        await _mark_purge_pending(session, device, reason=reason, purge_remote=purge_remote)
        log.error("media layer unavailable; keeping device %s for retry: %s", uid, exc)
        return {
            "ok": False,
            "uid": uid,
            "device_id": device_id,
            "error": "媒体层未就绪，清理待重试",
            "media": {},
        }

    try:
        media_result = await media_ingest.delete_device_data(
            session, device, await store().load(session), purge_remote=bool(purge_remote)
        )
    except Exception as exc:  # noqa: BLE001 - any media failure is retryable
        await _mark_purge_pending(session, device, reason=reason, purge_remote=purge_remote)
        log.error("media purge failed for %s; keeping record for retry: %s", uid, exc)
        await audit.record(
            session,
            action=audit.ACTION_DEVICE_DELETE,
            device=device,
            ip=ip,
            detail={"reason": reason, "ok": False, "error": str(exc)[:200]},
        )
        return {
            "ok": False,
            "uid": uid,
            "device_id": device_id,
            "error": str(exc)[:200] or "媒体清理失败",
            "media": {},
        }

    await audit.record(
        session,
        action=audit.ACTION_DEVICE_DELETE,
        device=device,
        ip=ip,
        detail={
            "reason": reason,
            "ok": True,
            "purge_remote": bool(purge_remote),
            "media": media_result,
        },
    )
    # Compare-and-set delete: a rotation that landed while the media step was
    # running (and therefore a *new* install in this row) must keep its record.
    conditions = [Device.id == device_id, Device.token_hash == seen_token]
    if seen_pending is None:
        conditions.append(Device.purge_pending_at_ms.is_(None))
    else:
        conditions.append(Device.purge_pending_at_ms == int(seen_pending))
    outcome = await session.execute(delete(Device).where(*conditions))
    if outcome.rowcount != 1:
        # 不 rollback：这里没有待提交的写入，而 rollback 会让同一 session 里其他
        # 已加载的 Device 过期，重试循环下一次访问它们就会触发同步懒加载。
        log.warning("device %s changed during purge; record kept", uid)
        return {**_stale_result(uid, device_id, "设备记录在清理期间被重新注册，记录已保留"), "media": media_result}
    await session.commit()

    try:
        from .events import EVENT_DEVICE_REMOVED, emit

        await emit(
            EVENT_DEVICE_REMOVED,
            device_uid=uid,
            data={"reason": reason, "purge_remote": bool(purge_remote)},
        )
    except Exception as exc:  # noqa: BLE001 - an event bus hiccup must not undo the delete
        log.warning("device removed event failed for %s: %s", uid, exc)

    log.info("device record purged uid=%s reason=%s media=%s", uid, reason, media_result)
    return {"ok": True, "uid": uid, "device_id": device_id, "media": media_result, "error": ""}


async def _row_unchanged(
    session: AsyncSession, device_id: int, token_hash: str, pending_at: int | None
) -> bool:
    """Whether the row still is the same incarnation that queued this cleanup.

    刻意按**列**读取而不是 ``select(Device)``：同一 session 的 identity map 会直接
    返回已加载的对象（不重新读库），那样这个见证比对就永远不会发现库里的轮换。
    """

    row = (
        await session.execute(
            select(Device.token_hash, Device.purge_pending_at_ms).where(Device.id == device_id)
        )
    ).first()
    if row is None:
        # 行已不在：上一个清理者删掉了它，没什么可做的。
        return False
    if str(row[0] or "") != token_hash:
        return False
    return row[1] == pending_at


def _stale_result(uid: str, device_id: int, error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "stale": True,
        "uid": uid,
        "device_id": device_id,
        "error": error,
        "media": {},
    }


async def _mark_purge_pending(
    session: AsyncSession,
    device: Device,
    *,
    reason: str,
    purge_remote: bool,
) -> None:
    """Flag the row (with its intent) so housekeeping retries this exact request."""

    device.purge_pending_at_ms = now_ms()
    device.purge_reason = str(reason)[:32]
    device.purge_remote = 1 if purge_remote else 0
    await session.commit()


async def retry_pending_purges(app: Any, session: AsyncSession, *, limit: int = 5) -> int:
    """Cleanup pass: finish the records flagged by a delete, retry failures.

    Selection is ``purge_reason``, not ``uninstalled_at_ms + purge_pending_at_ms``
    (S5): a manual delete of an offline device is not "uninstalled", so the old
    predicate never matched it and the promised background retry never ran.

    Guarded: the row must still be the incarnation that queued the task — a
    reinstall clears the mark (``enrol._rotate_and_activate``), so a device that
    came back between the enqueue and this tick is never deleted by a stale flag.
    ``purge_device_record`` re-checks the same witness before touching media.

    Capped per tick: a large backlog must not hold the single SQLite writer for the
    whole maintenance pass.
    """

    rows = list(
        (
            await session.execute(
                select(Device)
                .where(
                    Device.purge_pending_at_ms.is_not(None),
                    # v10 之前的老行由迁移回填，因此这里不会漏掉任何已排队的清理。
                    Device.purge_reason.is_not(None),
                )
                .order_by(Device.purge_pending_at_ms.asc())
                .limit(max(1, limit))
            )
        ).scalars().all()
    )
    done = 0
    for device in rows:
        reason = str(device.purge_reason or PURGE_REASON_AFTER_UNINSTALL)
        result = await purge_device_record(
            app,
            session,
            device,
            reason=reason,
            purge_remote=bool(device.purge_remote if device.purge_remote is not None else 1),
        )
        if result.get("ok"):
            done += 1
        else:
            # 失败/过期都不重试同一拍：purge_device_record 已经重新盖章（失败）或
            # 已被重装撤销（过期，此时标记已经不在了，下一拍自然不再选中）。
            log.debug("purge retry skipped for %s: %s", result.get("uid"), result.get("error"))
    return done


__all__ = [
    "PURGE_REASON_AFTER_UNINSTALL",
    "PURGE_REASON_MANUAL",
    "purge_device_record",
    "retry_pending_purges",
]
