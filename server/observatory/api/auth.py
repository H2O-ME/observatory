"""Console authentication.

* Password is either the bootstrap value from the environment or a bcrypt hash
  stored in the database once changed from the console.
* TOTP is optional, and **disabling it requires a valid code** — the previous
  build let any live session strip the second factor with an empty POST, which
  made the feature decorative.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import SESSION_COOKIE, client_ip, get_session, require_admin
from ..models import Setting, now_ms
from ..schemas import (
    LoginIn,
    LoginOut,
    OkOut,
    PasswordChangeIn,
    SessionOut,
    TotpDisableIn,
    TotpEnableIn,
    TotpSetupOut,
)
from ..security import (
    hash_password,
    issue_session,
    new_totp_secret,
    totp_uri,
    verify_password,
    verify_totp,
)
from ..services import audit
from ..services.runtime import Runtime, get_runtime

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

KEY_PASSWORD_HASH = "auth.password_hash"
KEY_TOTP_SECRET = "auth.totp_secret"


async def _get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(Setting, key)
    return None if row is None else str(row.value)


async def _put_setting(session: AsyncSession, key: str, value: str | None) -> None:
    if value is None:
        row = await session.get(Setting, key)
        if row is not None:
            await session.delete(row)
    else:
        row = await session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value, updated_at_ms=now_ms()))
        else:
            row.value = value
            row.updated_at_ms = now_ms()
    await session.commit()


async def _check_password(runtime: Runtime, session: AsyncSession, password: str) -> bool:
    stored_hash = await _get_setting(session, KEY_PASSWORD_HASH)
    if stored_hash:
        return verify_password(password, stored_hash)
    expected = runtime.settings.admin_password
    return hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))


@router.post("/login", response_model=LoginOut)
async def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> LoginOut:
    ip = client_ip(request)
    key = f"login:{ip}"
    if runtime.login_limiter and not runtime.login_limiter.allow(key):
        retry = runtime.login_limiter.retry_after(key)
        await audit.record(session, action=audit.ACTION_LOGIN_FAILED, ip=ip, detail={"reason": "rate_limited"})
        raise HTTPException(status_code=429, detail=f"尝试过于频繁，请 {retry} 秒后再试")

    if not await _check_password(runtime, session, payload.password):
        await audit.record(session, action=audit.ACTION_LOGIN_FAILED, ip=ip, detail={"reason": "bad_password"})
        raise HTTPException(status_code=401, detail="密码错误")

    secret = await _get_setting(session, KEY_TOTP_SECRET)
    if secret:
        if not payload.totp:
            raise HTTPException(status_code=401, detail="需要二次验证码")
        if not verify_totp(secret, payload.totp):
            await audit.record(session, action=audit.ACTION_LOGIN_FAILED, ip=ip, detail={"reason": "bad_totp"})
            raise HTTPException(status_code=401, detail="验证码错误")

    if runtime.login_limiter:
        runtime.login_limiter.reset(key)
    token, expires_at_ms = issue_session(
        secret_key=runtime.settings.secret_key,
        ttl_minutes=runtime.settings.session_ttl_minutes,
        version=runtime.version,
    )
    await audit.record(session, action=audit.ACTION_LOGIN, ip=ip, detail={"totp": bool(secret)})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=runtime.settings.session_ttl_minutes * 60,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    return LoginOut(token=token, expires_at_ms=expires_at_ms, version=runtime.version)


@router.post("/logout", response_model=OkOut)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> OkOut:
    await audit.record(session, action=audit.ACTION_LOGOUT, ip=client_ip(request))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return OkOut(message="已退出登录")


@router.get("/session", response_model=SessionOut)
async def session_state(
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> SessionOut:
    from ..deps import optional_admin

    payload = await optional_admin(request)
    secret = await _get_setting(session, KEY_TOTP_SECRET)
    return SessionOut(
        authenticated=payload is not None,
        totp_enabled=secret is not None,
        version=runtime.version,
        server_time_ms=now_ms(),
        console_host=runtime.settings.console_hosts[0] if runtime.settings.console_hosts else "",
        base_urls=runtime.settings.base_urls,
    )


@router.post("/totp/setup", response_model=TotpSetupOut)
async def totp_setup(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> TotpSetupOut:
    secret = new_totp_secret()
    await audit.record(session, action=audit.ACTION_TOTP_ENABLE, ip=client_ip(request), detail={"stage": "setup"})
    return TotpSetupOut(secret=secret, uri=totp_uri(secret))


@router.post("/totp/enable", response_model=OkOut)
async def totp_enable(
    payload: TotpEnableIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> OkOut:
    if not verify_totp(payload.secret, payload.code):
        raise HTTPException(status_code=400, detail="验证码不正确，请检查手机时间")
    await _put_setting(session, KEY_TOTP_SECRET, payload.secret)
    await audit.record(session, action=audit.ACTION_TOTP_ENABLE, ip=client_ip(request), detail={"stage": "enabled"})
    return OkOut(message="已开启二次验证")


@router.post("/totp/disable", response_model=OkOut)
async def totp_disable(
    payload: TotpDisableIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
) -> OkOut:
    secret = await _get_setting(session, KEY_TOTP_SECRET)
    if secret is None:
        return OkOut(message="二次验证本来就没有开启")
    if not verify_totp(secret, payload.code):
        await audit.record(
            session, action=audit.ACTION_TOTP_DISABLE, ip=client_ip(request), detail={"ok": False}
        )
        raise HTTPException(status_code=400, detail="验证码不正确，已拒绝关闭")
    await _put_setting(session, KEY_TOTP_SECRET, None)
    await audit.record(session, action=audit.ACTION_TOTP_DISABLE, ip=client_ip(request), detail={"ok": True})
    return OkOut(message="已关闭二次验证")


@router.post("/password", response_model=OkOut)
async def change_password(
    payload: PasswordChangeIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
    _admin: dict = Depends(require_admin),
) -> OkOut:
    if not await _check_password(runtime, session, payload.current_password):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
    await _put_setting(session, KEY_PASSWORD_HASH, hash_password(payload.new_password))
    await audit.record(session, action=audit.ACTION_PASSWORD_CHANGE, ip=client_ip(request))
    return OkOut(message="密码已更新")


_totp_select = select(Setting)


__all__ = ["KEY_PASSWORD_HASH", "KEY_TOTP_SECRET", "router"]