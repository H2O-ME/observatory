"""FastAPI dependencies: session, console auth, device auth, client IP."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Device
from .security import decode_session, device_token_hash
from .services.runtime import Runtime, get_runtime

log = logging.getLogger(__name__)

DEVICE_TOKEN_HEADER = "X-Device-Token"
SESSION_COOKIE = "obs_session"


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    runtime: Runtime = getattr(request.app.state, "runtime", None) or get_runtime()
    async with runtime.db.sessionmaker() as session:
        yield session


def get_app_runtime(request: Request) -> Runtime:
    return getattr(request.app.state, "runtime", None) or get_runtime()


def client_ip(request: Request) -> str:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is not None and runtime.settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def extract_bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def extract_session_token(request: Request) -> str:
    """Bearer header first, then the session cookie.

    The cookie exists so ``<img>``/``<video>`` tags can load frame content directly
    and therefore benefit from HTTP caching. A header-only scheme would force the
    console to fetch every frame through JS, which is what made scrubbing the
    timeline take a minute.
    """

    token = extract_bearer(request)
    if token:
        return token
    return request.cookies.get(SESSION_COOKIE, "")


async def require_admin(request: Request) -> dict:
    runtime = get_app_runtime(request)
    token = extract_session_token(request)
    payload = decode_session(token, secret_key=runtime.settings.secret_key)
    if payload is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return payload


async def optional_admin(request: Request) -> dict | None:
    runtime = get_app_runtime(request)
    token = extract_session_token(request)
    return decode_session(token, secret_key=runtime.settings.secret_key)


async def require_device(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Device:
    token = request.headers.get(DEVICE_TOKEN_HEADER, "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="缺少设备令牌")
    runtime = get_app_runtime(request)
    digest = device_token_hash(token, runtime.settings.secret_key)
    result = await session.execute(select(Device).where(Device.token_hash == digest))
    device = result.scalars().first()
    if device is None:
        raise HTTPException(status_code=401, detail="设备令牌无效")
    if device.uninstalled_at_ms:
        raise HTTPException(status_code=410, detail="该设备已卸载")
    return device


async def require_device_uid(
    request: Request,
    uid: str,
    session: AsyncSession = Depends(get_session),
) -> Device:
    result = await session.execute(select(Device).where(Device.uid == uid))
    device = result.scalars().first()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return device


def ws_admin_token(websocket) -> str:
    token = websocket.query_params.get("token", "")
    if token:
        return token
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return websocket.cookies.get(SESSION_COOKIE, "")


def ws_device_token(websocket) -> str:
    token = websocket.headers.get(DEVICE_TOKEN_HEADER.lower(), "")
    if not token:
        token = websocket.query_params.get("token", "")
    return token.strip()


__all__ = [
    "DEVICE_TOKEN_HEADER",
    "SESSION_COOKIE",
    "client_ip",
    "extract_bearer",
    "extract_session_token",
    "get_app_runtime",
    "get_session",
    "optional_admin",
    "require_admin",
    "require_device",
    "require_device_uid",
    "ws_admin_token",
    "ws_device_token",
]