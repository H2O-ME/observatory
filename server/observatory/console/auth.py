"""Console authentication: password + optional TOTP, session cookie / bearer.

The credential material lives in the ``settings`` table (``auth.password_hash``
and ``auth.totp_secret``) so a password change survives a container restart.
Everything is validated through ``core.crypto``; this module only owns the HTTP
surface and the audit trail.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.config import resolve_version
from ..core.crypto import (
    SESSION_COOKIE,
    RateLimiter,
    constant_time_compare,
    decode_session,
    hash_password,
    issue_session,
    new_totp_secret,
    totp_uri,
    verify_password,
    verify_totp,
)
from ..core.db import get_session
from ..core.errors import AppError
from ..core.models import Setting
from . import audit
from .deps import client_ip, extract_session_token, get_settings, require_admin
from .dto import stamp
from .envelope import ConsoleRoute

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"], route_class=ConsoleRoute)

KEY_PASSWORD_HASH = "auth.password_hash"
KEY_TOTP_SECRET = "auth.totp_secret"

_LOGIN_LIMITER: RateLimiter | None = None


def _login_limiter(settings: Any) -> RateLimiter:
    global _LOGIN_LIMITER
    if _LOGIN_LIMITER is None:
        _LOGIN_LIMITER = RateLimiter(settings.login_rate_limit, settings.login_rate_window_s)
    return _LOGIN_LIMITER


async def _get_auth_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(Setting, key)
    return None if row is None else str(row.value)


async def _put_auth_setting(session: AsyncSession, key: str, value: str | None) -> None:
    row = await session.get(Setting, key)
    if value is None:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(Setting(key=key, value=value, updated_at_ms=now_ms()))
    else:
        row.value = value
        row.updated_at_ms = now_ms()
    await session.commit()


async def _check_password(settings: Any, session: AsyncSession, password: str) -> bool:
    stored_hash = await _get_auth_setting(session, KEY_PASSWORD_HASH)
    if stored_hash:
        return verify_password(password, stored_hash)
    expected = settings.admin_password
    return constant_time_compare(password, expected)


@router.post("/login")
async def login(
    payload: dict[str, Any],
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    settings = get_settings(request)
    password = str(payload.get("password") or "")
    totp = payload.get("totp")
    ip = client_ip(request)
    key = f"login:{ip}"

    limiter = _login_limiter(settings)
    if not limiter.allow(key):
        await audit.record(
            session,
            action=audit.ACTION_LOGIN_FAILED,
            ip=ip,
            detail={"reason": "rate_limited"},
        )
        raise AppError.rate(f"尝试过于频繁，请 {limiter.retry_after(key)} 秒后再试")

    if not await _check_password(settings, session, password):
        await audit.record(
            session,
            action=audit.ACTION_LOGIN_FAILED,
            ip=ip,
            detail={"reason": "bad_password"},
        )
        raise AppError.auth("密码错误")

    secret = await _get_auth_setting(session, KEY_TOTP_SECRET)
    if secret:
        if not totp:
            raise AppError.auth("需要二次验证码")
        if not verify_totp(secret, str(totp)):
            await audit.record(
                session,
                action=audit.ACTION_LOGIN_FAILED,
                ip=ip,
                detail={"reason": "bad_totp"},
            )
            raise AppError.auth("验证码错误")

    limiter.reset(key)
    token, expires_at_ms = issue_session(
        secret_key=settings.secret_key,
        ttl_minutes=settings.session_ttl_minutes,
        version=resolve_version(),
    )
    await audit.record(session, action=audit.ACTION_LOGIN, ip=ip, detail={"totp": bool(secret)})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    return stamp({"token": token, "expires_at_ms": expires_at_ms, "version": resolve_version()})


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await audit.record(session, action=audit.ACTION_LOGOUT, ip=client_ip(request))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return stamp({"ok": True, "message": "已退出登录"})


@router.get("/session")
async def session_state(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    settings = get_settings(request)
    payload = decode_session(extract_session_token(request), secret_key=settings.secret_key)
    secret = await _get_auth_setting(session, KEY_TOTP_SECRET)
    return stamp(
        {
            "authenticated": payload is not None,
            "totp_enabled": secret is not None,
            "version": resolve_version(),
            "console_host": settings.console_hosts[0] if settings.console_hosts else "",
            "base_urls": list(settings.base_urls),
        }
    )


@router.post("/totp/setup")
async def totp_setup(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    secret = new_totp_secret()
    await audit.record(
        session,
        action=audit.ACTION_TOTP_ENABLE,
        ip=client_ip(request),
        detail={"stage": "setup"},
    )
    return stamp({"secret": secret, "uri": totp_uri(secret)})


@router.post("/totp/enable")
async def totp_enable(
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    secret = str(payload.get("secret") or "")
    code = str(payload.get("code") or "")
    if not verify_totp(secret, code):
        raise AppError.bad_request("验证码不正确，请检查手机时间")
    await _put_auth_setting(session, KEY_TOTP_SECRET, secret)
    await audit.record(
        session,
        action=audit.ACTION_TOTP_ENABLE,
        ip=client_ip(request),
        detail={"stage": "enabled"},
    )
    return stamp({"ok": True, "message": "已开启二次验证"})


@router.post("/totp/disable")
async def totp_disable(
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    secret = await _get_auth_setting(session, KEY_TOTP_SECRET)
    if secret is None:
        return stamp({"ok": True, "message": "二次验证本来就没有开启"})
    code = str(payload.get("code") or "")
    if not verify_totp(secret, code):
        await audit.record(
            session,
            action=audit.ACTION_TOTP_DISABLE,
            ip=client_ip(request),
            detail={"ok": False},
        )
        raise AppError.bad_request("验证码不正确，已拒绝关闭")
    await _put_auth_setting(session, KEY_TOTP_SECRET, None)
    await audit.record(
        session,
        action=audit.ACTION_TOTP_DISABLE,
        ip=client_ip(request),
        detail={"ok": True},
    )
    return stamp({"ok": True, "message": "已关闭二次验证"})


@router.post("/password")
async def change_password(
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    settings = get_settings(request)
    current = str(payload.get("current_password") or "")
    new = str(payload.get("new_password") or "")
    if not await _check_password(settings, session, current):
        raise AppError.bad_request("当前密码不正确")
    if len(new) < 10:
        raise AppError.bad_request("新密码至少 10 位")
    if new == current:
        raise AppError.bad_request("新密码不能与当前密码相同")
    await _put_auth_setting(session, KEY_PASSWORD_HASH, hash_password(new))
    await audit.record(session, action=audit.ACTION_PASSWORD_CHANGE, ip=client_ip(request))
    return stamp({"ok": True, "message": "密码已更新"})


# ``require_admin`` is imported from ``deps`` above; keep this module free of a
# second session/token decoder so the console has one auth path.
__all__ = [
    "KEY_PASSWORD_HASH",
    "KEY_TOTP_SECRET",
    "router",
    "session_state",
]