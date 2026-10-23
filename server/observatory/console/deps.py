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
    "client_ip",
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
    """Client IP, honouring ``trust_forwarded_for`` (default false, A11)."""

    settings = get_settings(request)
    if getattr(settings, "trust_forwarded_for", False):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def extract_session_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            return token
    return request.cookies.get(SESSION_COOKIE, "")


async def require_admin(request: Request) -> dict:
    settings = get_settings(request)
    payload = decode_session(extract_session_token(request), secret_key=settings.secret_key)
    if payload is None:
        raise AppError.auth("未登录或会话已过期")
    return payload