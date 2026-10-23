"""Console settings: the only HTTP surface that writes ``settings`` rows.

Outbound configuration is always ``device_safe_config(effective)`` (A4).  The
server-internal effective view (which may contain ``SECRET_KEYS``) never leaves
this module except through that filter.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import resolve_version
from ..core.settings_spec import SettingsError, describe, public_view
from ..core.db import get_session
from ..core.errors import AppError
from ..core.models import Device
from . import audit
from .deps import client_ip, get_links, get_settings, require_admin, store
from .dto import device_safe_config, stamp
from .envelope import ConsoleRoute

log = logging.getLogger(__name__)

router = APIRouter(tags=["settings"], dependencies=[Depends(require_admin)], route_class=ConsoleRoute)

#: A config push is small and local; it does not need a command-type budget.
CONFIG_PUSH_DEADLINE_MS = 5_000


async def push_config(
    session: AsyncSession,
    *,
    links: Any,
    device: Device | None = None,
) -> int:
    """Push the device-safe effective config to one or all connected agents.

    Returns the number of sessions the push was attempted on.  A broken carrier
    only produces a warning: the next heartbeat/welcome carries the same config
    anyway, and failing the settings write because one device is half-connected
    would be worse.
    """

    targets = []
    if device is not None:
        targets.append((device.id, device))
    else:
        rows = (await session.execute(select(Device))).scalars().all()
        targets = [(row.id, row) for row in rows]

    settings_values = store()
    sent = 0
    for device_id, row in targets:
        agent_session = links.get(device_id)
        if agent_session is None:
            continue
        effective = await settings_values.effective(session, row.settings or {})
        try:
            await agent_session.send(
                "config",
                {"config": device_safe_config(effective)},
                deadline_ms=CONFIG_PUSH_DEADLINE_MS,
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001 - carrier failure must not fail the write
            log.warning("config push failed device=%s: %s", row.uid, exc)
    return sent


@router.get("/settings")
async def get_settings_payload(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    settings = get_settings(request)
    values = await store().load(session)
    return stamp(
        {
            "values": public_view(values),
            "specs": describe(),
            "server_version": resolve_version(),
            "console_host": settings.console_hosts[0] if settings.console_hosts else "",
            "base_urls": list(settings.base_urls),
        }
    )


@router.patch("/settings")
async def patch_settings(
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    raw = payload.get("values")
    if not isinstance(raw, dict) or not raw:
        raise AppError.bad_request("values 必须是非空对象")
    try:
        changed = await store().patch(session, raw)
    except SettingsError as exc:
        raise AppError.bad_request(str(exc)) from exc

    await audit.record(
        session,
        action=audit.ACTION_SETTINGS_UPDATE,
        ip=client_ip(request),
        detail={"keys": sorted(changed.keys())},
    )
    values = await store().load(session)
    sent = await push_config(session, links=get_links(request))
    log.info("settings updated keys=%s pushed=%d", sorted(changed), sent)
    return stamp(
        {
            "values": public_view(values),
            "specs": describe(),
            "server_version": resolve_version(),
            "pushed_to": sent,
        }
    )


@router.get("/settings/effective/{uid}")
async def get_effective_settings(
    uid: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Server-internal effective view is not exposed; this is the safe view."""

    device = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()
    if device is None:
        raise AppError.gone("设备不存在")
    effective = await store().effective(session, device.settings or {})
    return stamp({"uid": uid, "settings": device_safe_config(effective)})


__all__ = ["CONFIG_PUSH_DEADLINE_MS", "push_config", "router"]