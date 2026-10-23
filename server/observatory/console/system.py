"""Detailed runtime status for the console.

``GET /api/v1/health`` and ``GET /api/v1/version`` belong to ``main.py`` on
purpose: the liveness surface must stay reachable even if the console package
fails to import.  This router only serves the *detailed* page behind login.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.config import resolve_version
from ..core.db import get_session
from ..core.models import Command, Device, Frame
from .deps import get_journal, get_links, get_settings, require_admin
from .dto import stamp
from .envelope import ConsoleRoute
from .install import install_info

log = logging.getLogger(__name__)

router = APIRouter(tags=["system"], dependencies=[Depends(require_admin)], route_class=ConsoleRoute)

_PROCESS_STARTED_AT_MS = now_ms()


def _tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            log.warning("could not stat %s while computing storage usage", path)
    return total


@router.get("/system")
async def system_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    settings = get_settings(request)
    journal = get_journal(request)
    links = get_links(request)

    device_count = int((await session.execute(select(func.count(Device.id)))).scalar() or 0)
    frame_count = int((await session.execute(select(func.count(Frame.id)))).scalar() or 0)
    command_count = int((await session.execute(select(func.count(Command.id)))).scalar() or 0)
    pending_commands = int(journal.open_count())

    frames_dir = Path(getattr(settings, "frames_dir", Path(settings.data_dir) / "frames"))
    thumbs_dir = Path(getattr(settings, "thumbs_dir", Path(settings.data_dir) / "thumbs"))
    db_path = Path(getattr(settings, "db_path", Path(settings.data_dir) / "observatory.db"))
    try:
        db_bytes = db_path.stat().st_size
    except OSError:
        log.warning("could not stat database at %s", db_path)
        db_bytes = 0
    try:
        disk = shutil.disk_usage(settings.data_dir)
        disk_total, disk_free = disk.total, disk.free
    except OSError as exc:
        log.warning("could not read disk usage for %s: %s", settings.data_dir, exc)
        disk_total, disk_free = 0, 0

    started_at_ms = int(getattr(request.app.state, "started_at_ms", _PROCESS_STARTED_AT_MS) or _PROCESS_STARTED_AT_MS)
    from .control import SESSIONS

    return stamp(
        {
            "version": resolve_version(),
            "uptime_s": max(0, (now_ms() - started_at_ms) // 1000),
            "devices": device_count,
            "frames": frame_count,
            "commands": command_count,
            "pending_commands": pending_commands,
            "channels": links.counts(),
            "control_sessions": SESSIONS.active_count(),
            "storage": {
                "frames_bytes": _tree_bytes(frames_dir),
                "thumbs_bytes": _tree_bytes(thumbs_dir),
                "db_bytes": db_bytes,
                "disk_total": disk_total,
                "disk_free": disk_free,
            },
            "base_urls": list(settings.base_urls),
            "console_host": settings.console_hosts[0] if settings.console_hosts else "",
            "install": await install_info(request, session),
        }
    )


__all__ = ["router"]