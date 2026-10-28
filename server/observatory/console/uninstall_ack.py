"""Uninstall confirmation: the only place a device is marked uninstalled (P2).

Why this module exists
----------------------
Before P2 the confirmation arrived as an ``uninstall_ack`` *uplink envelope* and
``main.py`` handled it inline. That works, but it cannot be used as a
synchronous, retryable confirmation, and the envelope path is the one the agent's
outbox would have to carry — while the purge helper is about to delete the
outbox file. ``POST /api/v1/agent/uninstall/ack`` therefore exists: the agent
sends it as a plain authenticated request and gets an answer, so "the server has
persisted the result" is a fact the agent can act on before deleting data.

The outbox envelope stays supported for 0.3.7 agents (legacy path), and both
paths land here so there is exactly one implementation of "confirm uninstall".

What a confirmation does
------------------------
1. settle the command to ``ok`` (only real evidence may do this; ``unconfirmed``
   is upgraded and logged),
2. stamp ``devices.uninstalled_at_ms``,
3. audit ``device.uninstall`` with the stage and the source,
4. drop the live session and revoke any control grant.

Deleting the device *record* is **not** done here (P3): the row must survive
long enough for the operator to see what happened, and command history has to be
preserved before that delete becomes safe.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.crypto import DEVICE_TOKEN_HEADER
from ..core.db import get_session
from ..core.errors import AppError
from ..core.log import log_event
from ..core.models import Device
from . import audit
from .deps import client_ip
from .dto import stamp

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent-uninstall"])


async def confirm_uninstall(
    app: Any,
    session: AsyncSession,
    device: Device,
    *,
    command_id: str | None = None,
    source: str,
    stage: str = "confirmed",
    helper_pid: int | None = None,
    ip: str = "",
) -> dict[str, Any]:
    """Settle the uninstall command and record the lifecycle fact.

    ``command_id`` is the exact command (new agents). Legacy agents cannot supply
    one, so the single outstanding uninstall is used — and if there is more than
    one, nothing is settled (the ambiguity is audited instead of guessed).
    """

    journal = getattr(app.state, "journal", None)
    resolved_id = command_id
    ambiguous = False
    settled = False

    if journal is not None:
        if resolved_id:
            record, settled = await journal.settle_ack(resolved_id, result={"acked": True, "source": source})
        else:
            open_command = await journal.open_uninstall(int(device.id))
            if open_command is not None:
                record, settled = await journal.settle_ack(
                    open_command.id, result={"acked": True, "source": source}
                )
                resolved_id = open_command.id
            else:
                ambiguous = True

    if not settled and not ambiguous and resolved_id:
        # The row exists but was already terminal in a way settle_ack refuses
        # (e.g. cancelled). Record what we saw instead of pretending success.
        log_event(
            log,
            logging.WARNING,
            "uninstall.ack_not_settled",
            device_id=int(device.id),
            command_id=resolved_id,
        )

    previous_uninstalled = device.uninstalled_at_ms
    if not previous_uninstalled:
        device.uninstalled_at_ms = now_ms()
        await session.commit()

    detail: dict[str, Any] = {
        "stage": stage,
        "source": source,
        "command_id": resolved_id or "",
        "protocol": 3,
    }
    if helper_pid:
        detail["helper_pid"] = int(helper_pid)
    if previous_uninstalled:
        detail["already_uninstalled_at_ms"] = int(previous_uninstalled)
    if ambiguous:
        detail["ambiguous"] = "no open uninstall command for this device"
    await audit.record(
        session,
        action=audit.ACTION_DEVICE_UNINSTALL,
        device=device,
        device_uid=str(device.uid or ""),
        ip=ip,
        detail=detail,
    )

    links = getattr(app.state, "links", None)
    if links is not None:
        try:
            await links.unregister(int(device.id))
        except Exception as exc:  # noqa: BLE001 - confirmation must not fail on teardown
            log_event(log, logging.WARNING, "uninstall.unregister_failed", device_id=int(device.id), error=str(exc))

    from .control import revoke_device_grants

    try:
        await revoke_device_grants(app, int(device.id), reason="device uninstalled")
    except Exception as exc:  # noqa: BLE001 - grants are a separate concern
        log_event(log, logging.WARNING, "uninstall.revoke_failed", device_id=int(device.id), error=str(exc))

    # P3: a *confirmed* uninstall removes the activity record, so the console list
    # matches reality and a later reinstall registers cleanly. Command history and
    # the audit above survive (v8: commands.device_id is SET NULL, not CASCADE).
    # Only a settled ACK reaches here — a timeout never deletes anything.
    #
    # The purge is *marked*, not executed inline: an agent whose response was lost
    # retries this endpoint, and deleting the row here would make that retry a 401
    # (the credential is gone with the row) — i.e. idempotency would be broken by
    # our own success. Marking it lets housekeeping remove the record one tick
    # later, which is prompt enough (the device already shows as uninstalled) and
    # keeps "retry the ACK" answerable while the row still exists.
    marked = False
    if settled and _auto_purge_enabled(app) and not device.purge_pending_at_ms:
        device.purge_pending_at_ms = now_ms()
        await session.commit()
        marked = True
        log_event(
            log,
            logging.INFO,
            "uninstall.purge_marked",
            device_id=int(device.id),
            uid=str(device.uid or ""),
        )

    log_event(
        log,
        logging.INFO,
        "uninstall.confirmed",
        device_id=int(device.id),
        uid=str(device.uid or ""),
        command_id=resolved_id or "",
        settled=settled,
        source=source,
        purge_marked=marked,
    )
    return {
        "command_id": resolved_id or "",
        "settled": settled,
        "uninstalled": True,
        "ambiguous": ambiguous,
        # The record is removed by the cleanup pass, not by this call.
        "record_removed": False,
        "purge_pending": marked or bool(device.purge_pending_at_ms),
    }


def _auto_purge_enabled(app: Any) -> bool:
    """Kill switch for the automatic record removal (default on, P3)."""

    return bool(getattr(app.state.settings, "uninstall_auto_purge_device", True))


@router.post("/uninstall/ack")
async def uninstall_ack(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Synchronous, idempotent uninstall confirmation (new agents).

    Idempotent on purpose: the agent retries this request when the network drops,
    and a repeat must not create a second fact or fail. The device token is the
    credential (never the UID), so only the device itself can confirm its own
    uninstall.
    """

    token = request.headers.get(DEVICE_TOKEN_HEADER, "").strip()
    if not token:
        raise AppError.auth("缺少设备令牌")
    from ..core import device_auth

    # allow_uninstalled: this endpoint must stay idempotent *after* it marked the
    # device uninstalled, otherwise its own retry (response lost) would answer 410
    # and the agent would treat a successful purge as a terminal dead link.
    device = await device_auth.load_device(
        session, token, settings=request.app.state.settings, allow_uninstalled=True
    )
    if device is None:
        raise AppError.auth("设备令牌无效")
    command_id = str(payload.get("command_id") or "").strip() or None
    stage = str(payload.get("stage") or "confirmed")[:32]
    raw_pid = payload.get("helper_pid")
    helper_pid = int(raw_pid) if isinstance(raw_pid, int) and raw_pid > 0 else None

    result = await confirm_uninstall(
        request.app,
        session,
        device,
        command_id=command_id,
        source="ack_endpoint",
        stage=stage,
        helper_pid=helper_pid,
        ip=client_ip(request),
    )
    return stamp(result)


def install_router() -> APIRouter:
    return router


__all__ = ["confirm_uninstall", "router"]
