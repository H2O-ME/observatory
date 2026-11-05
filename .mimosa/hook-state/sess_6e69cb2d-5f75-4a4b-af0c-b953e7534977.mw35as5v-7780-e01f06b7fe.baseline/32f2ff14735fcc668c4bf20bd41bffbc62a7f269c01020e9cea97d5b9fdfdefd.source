"""Shared FastAPI dependencies for the console and the legacy compatibility layer.

Everything here reads from ``request.app.state`` -- the assembly in ``main.py``
owns the singletons (``settings``, ``db``, ``journal``, ``links``).  The console
does not build its own database or registry, so there is exactly one instance of
each fact in the process.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..core.crypto import SESSION_COOKIE, decode_session
from ..core.errors import AppError

__all__ = [
    "check_console_session",
    "client_ip",
    "evaluate_session",
    "extract_session_token",
    "get_journal",
    "get_links",
    "get_settings",
    "require_admin",
    "store",
]


def get_settings(request: Request) -> Any:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise RuntimeError("app.state.settings is not initialised")
    return settings


def get_journal(request: Request) -> Any:
    journal = getattr(request.app.state, "journal", None)
    if journal is None:
        raise RuntimeError("app.state.journal is not initialised")
    return journal


def get_links(request: Request) -> Any:
    links = getattr(request.app.state, "links", None)
    if links is None:
        # ``link.registry.registry`` is the same object main stores on app.state.
        from ..link.registry import registry as links

    return links


def store() -> Any:
    """Process-wide settings store (DB-backed with an in-memory cache)."""

    from .settings_store import get_store

    return get_store()


def client_ip(request: Request) -> str:
    """Client IP, honouring ``trust_forwarded_for`` (default false, A11).

    Uses the **rightmost** X-Forwarded-For entry (0.3.1 F16): that value was
    appended by the proxy we trust, so a client cannot forge it by sending its
    own leftmost entries — the leftmost pick made per-IP rate limiting pointless.
    """

    settings = get_settings(request)
    if getattr(settings, "trust_forwarded_for", False):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else ""


def extract_session_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            return token
    return request.cookies.get(SESSION_COOKIE, "")


def evaluate_session(token: str, *, secret_key: str, current_pwv: int) -> tuple[dict | None, str]:
    """唯一的"这个会话现在还算不算登录"判定（S6，2026-10 独立审计）。

    JWT 签名与过期只是必要条件：密码/TOTP 变更会 bump 凭据版本（F15），
    旧令牌的 ``pwv`` 与进程内当前值不一致时必须视为已撤销。此前只有
    ``require_admin`` 做这一步，``/auth/session`` 只 decode，于是改密后
    session 接口说"已登录"、普通 API 却回 401，前端在两个状态之间反复
    跳转刷新。现在 decode + 版本校验只在这里实现一次。

    返回 ``(payload, 失败原因)``：payload 非空表示会话当前有效。
    """

    payload = decode_session(token, secret_key=secret_key)
    if payload is None:
        return None, "未登录或会话已过期"
    if int(payload.get("pwv", 0) or 0) != int(current_pwv or 0):
        return None, "凭据已变更，请重新登录"
    return payload, ""


def check_console_session(request: Request) -> tuple[dict | None, str]:
    """:func:`evaluate_session` 的 HTTP 入口：从 cookie/bearer 取令牌。"""

    settings = get_settings(request)
    return evaluate_session(
        extract_session_token(request),
        secret_key=settings.secret_key,
        current_pwv=int(getattr(request.app.state, "credential_version", 0) or 0),
    )


async def require_admin(request: Request) -> dict:
    payload, reason = check_console_session(request)
    if payload is None:
        # F15: 密码/TOTP 变更后签名仍有效的旧令牌也必须当作未登录。
        raise AppError.auth(reason)
    return payload
