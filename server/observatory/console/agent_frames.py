"""The single device frame upload endpoint (HTTP surface lives in console).

``POST /api/v1/agent/frames`` is the only HTTP ingestion entry for both agent
generations:

* 0.2.x sends ``ts_ms``;
* 0.3.0 sends ``device_ts_ms`` (§5.6);
* both may send ``X-Frame-Id`` for idempotency.

The handler authenticates the device token, then calls exactly one ingestion
function, ``media.ingest.store_frame`` (C2). The legacy compatibility module does
not register this path anymore, so there is no duplicate route and no second
storage path.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.crypto import DEVICE_TOKEN_HEADER, device_token_hash
from ..core.db import get_session
from ..core.errors import AppError
from ..core.models import Device
from ..core.crypto import RateLimiter
from .dto import stamp

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent-frames"])

#: F53: per-device frame-ingress bucket. After a server restart the whole fleet
#: replays persisted frames at once; without a bucket that storm goes straight
#: into transcode + SQLite and stacks on the reconnect burst.
FRAMES_RATE_LIMIT = 240
FRAMES_RATE_WINDOW_S = 60
_FRAMES_LIMITER = RateLimiter(FRAMES_RATE_LIMIT, FRAMES_RATE_WINDOW_S)


async def require_device_token(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Device:
    token = request.headers.get(DEVICE_TOKEN_HEADER, "").strip()
    if not token:
        raise AppError.auth("缺少设备令牌")
    digest = device_token_hash(token, request.app.state.settings.secret_key)
    device = (await session.execute(select(Device).where(Device.token_hash == digest))).scalars().first()
    if device is None:
        raise AppError.auth("设备令牌无效")
    if device.uninstalled_at_ms:
        raise AppError.gone("该设备已卸载")
    return device


@router.post("/frames")
async def upload_frame(
    request: Request,
    ts_ms: int = Query(default=0, ge=0, description="0.2.x capture timestamp (ms)"),
    device_ts_ms: int = Query(default=0, ge=0, description="0.3.0 capture timestamp (ms)"),
    session_id: int = Query(default=0, ge=0),
    kind: str = Query(default="screen"),
    session: AsyncSession = Depends(get_session),
    device: Device = Depends(require_device_token),
) -> dict[str, Any]:
    settings = request.app.state.settings
    body = await request.body()
    if not body:
        raise AppError.bad_request("空图像")
    if len(body) > int(settings.max_frame_bytes):
        raise AppError.bad_request("图像过大")
    if not _FRAMES_LIMITER.allow(f"frames:{device.uid}"):
        raise AppError.rate(
            "帧上传过于频繁，请稍后重试",
            retry_after=_FRAMES_LIMITER.retry_after(f"frames:{device.uid}"),
        )

    try:
        from ..media import ingest as media_ingest
    except ImportError as exc:  # pragma: no cover - W0 skeleton guard
        raise AppError.internal("媒体层未就绪，无法接收画面") from exc

    # 0.3.0 field wins; the old query name is accepted for one compatibility version.
    capture_ts = int(device_ts_ms or ts_ms or 0) or None
    frame = await media_ingest.store_frame(
        session,
        device,
        body,
        frame_id=request.headers.get("X-Frame-Id") or None,
        device_ts_ms=capture_ts,
        session_id=int(session_id or 0),
        kind=kind if kind in {"screen", "camera"} else "screen",
        settings=await _media_settings(session),
    )
    if frame is None:
        raise AppError.bad_request("图像无法解码、超出限制或落盘失败")

    # Legacy layer is allowed to refresh last_seen; the v3 ingress deliberately
    # leaves presence to the link/presence code (media owns no presence write).
    if int(getattr(device, "protocol", 3) or 3) < 3:
        device.last_seen_ms = now_ms()
        await session.commit()

    return stamp(
        {
            "frame_id": frame.id,
            "ts_ms": int(frame.ts_ms),
            "device_time_ms": int(frame.device_ts_ms) if frame.device_ts_ms is not None else None,
            "device_ts_ms": int(frame.device_ts_ms) if frame.device_ts_ms is not None else None,
            "stored": True,
        }
    )


async def _media_settings(session: AsyncSession) -> Any:
    try:
        from ..media import settings as media_settings
    except ImportError as exc:  # pragma: no cover - W0 skeleton guard
        raise AppError.internal("媒体配置未就绪") from exc
    return await media_settings.load(session)


__all__ = ["require_device_token", "router"]