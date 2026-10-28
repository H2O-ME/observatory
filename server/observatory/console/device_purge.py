"""Removing a device's activity record, with retry (P3).

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
flagged with ``purge_pending_at_ms`` so housekeeping can retry: reporting a clean
delete while media remains would be exactly the dishonest state C6 forbids. The
flag is not a ban — the row disappears as soon as a retry succeeds, and a
reinstall is unaffected because enrolment looks at the UID, not at leftovers.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.models import Device
from . import audit

log = logging.getLogger(__name__)


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
    ``purge_pending_at_ms`` is set for the retry pass.
    """

    device_id = int(device.id)
    uid = str(device.uid or "")

    links = getattr(app.state, "links", None)
    if links is not None:
        await links.unregister(device_id)

    try:
        from .control import revoke_device_grants

        await revoke_device_grants(app, device_id, reason=f"device {reason}")
    except Exception as exc:  # noqa: BLE001 - grants are in-memory bookkeeping
        log.warning("revoke grants failed for %s: %s", uid, exc)

    try:
        from ..media import ingest as media_ingest
        from .deps import store
    except ImportError as exc:  # pragma: no cover - media layer optional at import time
        await _mark_purge_pending(session, device)
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
        await _mark_purge_pending(session, device)
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
    await session.delete(device)
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


async def _mark_purge_pending(session: AsyncSession, device: Device) -> None:
    """Flag the row so housekeeping retries instead of silently forgetting it."""

    device.purge_pending_at_ms = now_ms()
    await session.commit()


async def retry_pending_purges(app: Any, session: AsyncSession, *, limit: int = 5) -> int:
    """Cleanup pass: remove records flagged by a confirmed uninstall, retry failures.

    Guarded: a row is only removed while it still carries ``uninstalled_at_ms``.
    A reinstall clears both marks (``enrol._rotate_and_activate``), so a device that
    came back between the ACK and this tick is never deleted by a stale flag — it
    would otherwise lose a *live* device record.

    Capped per tick: a large backlog must not hold the single SQLite writer for the
    whole maintenance pass.
    """

    rows = list(
        (
            await session.execute(
                select(Device)
                .where(
                    Device.purge_pending_at_ms.is_not(None),
                    # Never purge a device that is not actually marked uninstalled.
                    Device.uninstalled_at_ms.is_not(None),
                )
                .order_by(Device.purge_pending_at_ms.asc())
                .limit(max(1, limit))
            )
        ).scalars().all()
    )
    done = 0
    for device in rows:
        result = await purge_device_record(
            app, session, device, reason="auto_after_uninstall", purge_remote=True
        )
        if result.get("ok"):
            done += 1
        else:
            # Clear the mark only when the row may legitimately be retried later:
            # keep it set so the next tick tries again, but stop hammering inside
            # this pass (purge_device_record already re-stamped it).
            log.debug("purge retry failed for %s: %s", result.get("uid"), result.get("error"))
    return done


__all__ = ["purge_device_record", "retry_pending_purges"]
