"""Installer artifacts, one-time install tokens and package downloads.

A18 replaced the shared enrolment code inside the download with a
single-use token:

* an admin mints a token (TTL, one use, bound to the first device that redeems
  it); only the HMAC hash is stored;
* downloading ``install.ps1`` / ``install.cmd`` only *validates* the token -- it
  is consumed at enrolment (``console/enroll.py``), so a leaked download cannot
  be replayed after the first machine uses it;
* the shared ``OBS_ENROLL_CODE`` path was removed in 0.3.1 together with the
  0.2.x protocol layer; credentials are tokens only.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.config import resolve_version
from ..core.crypto import device_token_hash
from ..core.db import get_session
from ..core.errors import AppError
from ..core.models import InstallToken
from . import audit
from .deps import client_ip, get_settings, require_admin, store
from .dto import stamp
from .envelope import ConsoleRoute
from .updates import service

log = logging.getLogger(__name__)

router = APIRouter(tags=["install"], dependencies=[Depends(require_admin)], route_class=ConsoleRoute)
public_router = APIRouter(prefix="/public", tags=["public"])

DEFAULT_TOKEN_TTL_S = 1800
MIN_TOKEN_TTL_S = 60
MAX_TOKEN_TTL_S = 24 * 3600
MAX_TOKEN_USES = 1


def _template_dir() -> Path:
    """Templates live in ``server/templates`` after the 0.3.0 layout move."""

    here = Path(__file__).resolve()
    candidates = (
        here.parents[2] / "templates",  # server/templates
        here.parents[1] / "templates",  # server/observatory/templates (0.2.x)
    )
    for candidate in candidates:
        if (candidate / "install.ps1").exists() or (candidate / "install.cmd").exists():
            return candidate
    raise AppError.internal("安装脚本模板缺失")


def _read_template(name: str) -> str:
    path = _template_dir() / name
    if not path.exists():
        raise AppError.internal(f"安装脚本模板缺失: {name}")
    return path.read_text(encoding="utf-8")


def _token_hash(token: str, settings: Any) -> str:
    return device_token_hash(token, settings.secret_key)


def _substitute(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered


def render_ps1(request: Request, token: str) -> str:
    settings = get_settings(request)
    template = _read_template("install.ps1")
    quoted = ", ".join(f"'{url}'" for url in settings.base_urls)
    replacements = {
        "@@VERSION@@": resolve_version(),
        "@@ENROLL_CODE@@": token,
        "@@INSTALL_TOKEN@@": token,
        "@@ENROLL_CODE_URL@@": quote(token, safe=""),
        "@@INSTALL_TOKEN_URL@@": quote(token, safe=""),
        "@@BASE_URLS@@": quoted,
        "@@CONSOLE_HOST@@": settings.console_hosts[0] if settings.console_hosts else "",
        "@@SERVER_TIME_MS@@": str(now_ms()),
    }
    # Windows PowerShell 5.1 decodes a BOM-less .ps1 as ANSI; the download
    # workflow requires the BOM (the `irm | iex` form ignores it).
    return "\ufeff" + _substitute(template, replacements)


def render_cmd(request: Request, token: str) -> str:
    settings = get_settings(request)
    template = _read_template("install.cmd")
    url_lines = "\n".join(
        f'set "URL{index}={url}"' for index, url in enumerate(settings.base_urls, start=1)
    )
    replacements = {
        "@@VERSION@@": resolve_version(),
        "@@ENROLL_CODE@@": token,
        "@@INSTALL_TOKEN@@": token,
        "@@ENROLL_CODE_URL@@": quote(token, safe=""),
        "@@INSTALL_TOKEN_URL@@": quote(token, safe=""),
        "@@URL_LINES@@": url_lines,
    }
    return _substitute(template, replacements)


async def install_info(request: Request, session: AsyncSession) -> dict[str, Any]:
    settings = get_settings(request)
    values = await store().load(session)
    available = int(
        (
            await session.execute(
                select(func.count(InstallToken.id)).where(
                    InstallToken.expires_at_ms > now_ms(),
                    InstallToken.used_at_ms.is_(None),
                )
            )
        ).scalar()
        or 0
    )
    token_hint = "/api/v1/install/tokens"
    return {
        "enabled": available > 0,
        "enroll_required": True,
        "available_tokens": available,
        "token_ttl_minutes": int(values.get("install_token_ttl_minutes", 1440)),
        "version": resolve_version(),
        "console_host": settings.console_hosts[0] if settings.console_hosts else "",
        "base_urls": list(settings.base_urls),
        "install_token_endpoint": token_hint,
        "script_url": "/api/v1/public/install.ps1",
        "cmd_url": "/api/v1/public/install.cmd",
        "artifacts": [
            {
                "kind": "cmd",
                "label": "安装程序 (.cmd)",
                "filename": f"Observatory-Install-{resolve_version()}.cmd",
                "url": "/api/v1/public/install.cmd?token=<一次性令牌>",
                "hint": "在「安装令牌」页生成一次性令牌后下载，双击运行",
            },
            {
                "kind": "ps1",
                "label": "安装脚本 (.ps1)",
                "filename": f"Observatory-Install-{resolve_version()}.ps1",
                "url": "/api/v1/public/install.ps1?token=<一次性令牌>&download=1",
                "hint": "批量部署/组策略用，需加一次性令牌参数",
            },
        ],
    }


async def validate_install_token(session: AsyncSession, token: str, *, settings: Any) -> InstallToken:
    """Validate without consuming; consumption happens at enrolment (A18)."""

    digest = _token_hash(token, settings)
    row = (
        await session.execute(select(InstallToken).where(InstallToken.token_hash == digest))
    ).scalars().first()
    if row is None:
        raise AppError.auth("安装令牌无效")
    if int(row.expires_at_ms or 0) <= now_ms():
        raise AppError.auth("安装令牌已过期")
    if row.used_at_ms is not None and int(row.max_uses or 1) <= 1:
        raise AppError.auth("安装令牌已被使用")
    return row


async def require_install_credential(
    request: Request,
    session: AsyncSession,
    *,
    token: str,
) -> str:
    """Validate the download credential (token only; consumption happens at enrolment)."""

    settings = get_settings(request)
    if not token:
        raise AppError.auth("缺少安装令牌")
    await validate_install_token(session, token, settings=settings)
    return token


@router.post("/install-tokens")
@router.post("/install/tokens", include_in_schema=False)
async def create_install_token(
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    settings = get_settings(request)
    values = await store().load(session)
    if payload.get("ttl_s") is None:
        ttl_s = int(values.get("install_token_ttl_minutes", DEFAULT_TOKEN_TTL_S // 60)) * 60
    else:
        try:
            ttl_s = int(payload["ttl_s"])
        except (TypeError, ValueError) as exc:
            raise AppError.bad_request("ttl_s 必须是整数") from exc
    if ttl_s < MIN_TOKEN_TTL_S or ttl_s > MAX_TOKEN_TTL_S:
        raise AppError.bad_request(f"ttl_s 必须在 {MIN_TOKEN_TTL_S}..{MAX_TOKEN_TTL_S} 秒之间")
    plaintext = secrets.token_urlsafe(32)
    row = InstallToken(
        token_hash=_token_hash(plaintext, settings),
        created_by=str((await _admin_id(request)) or "console"),
        created_at_ms=now_ms(),
        expires_at_ms=now_ms() + ttl_s * 1000,
        used_at_ms=None,
        used_device_id=None,
        max_uses=MAX_TOKEN_USES,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await audit.record(
        session,
        action=audit.ACTION_INSTALL_TOKEN_CREATE,
        ip=client_ip(request),
        detail={"token_id": row.id, "ttl_s": ttl_s},
    )
    return stamp(
        {
            "id": row.id,
            "token": plaintext,
            "expires_at_ms": row.expires_at_ms,
            "max_uses": row.max_uses,
            "install_cmd_url": f"/api/v1/public/install.cmd?token={quote(plaintext, safe='')}",
            "install_ps1_url": f"/api/v1/public/install.ps1?token={quote(plaintext, safe='')}&download=1",
        }
    )


async def _admin_id(request: Request) -> str:
    from ..core.crypto import decode_session
    from .deps import extract_session_token, get_settings

    payload = decode_session(extract_session_token(request), secret_key=get_settings(request).secret_key)
    return str((payload or {}).get("sub") or "console")[:64]


@router.get("/install-tokens")
@router.get("/install/tokens", include_in_schema=False)
async def list_install_tokens(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = list((await session.execute(select(InstallToken).order_by(InstallToken.id.desc()))).scalars().all())
    return stamp(
        {
            "tokens": [
                {
                    "id": row.id,
                    "created_by": row.created_by,
                    "created_at_ms": row.created_at_ms,
                    "expires_at_ms": row.expires_at_ms,
                    "used_at_ms": row.used_at_ms,
                    "used_device_id": row.used_device_id,
                    "max_uses": row.max_uses,
                    "expired": int(row.expires_at_ms or 0) <= now_ms(),
                    "used": row.used_at_ms is not None,
                }
                for row in rows
            ]
        }
    )


@router.delete("/install-tokens/{token_id}")
@router.delete("/install/tokens/{token_id}", include_in_schema=False)
async def delete_install_token(
    token_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await session.get(InstallToken, token_id)
    if row is None:
        raise AppError.gone("安装令牌不存在")
    await session.delete(row)
    await session.commit()
    await audit.record(
        session,
        action=audit.ACTION_INSTALL_TOKEN_DELETE,
        ip=client_ip(request),
        detail={"token_id": token_id},
    )
    return stamp({"ok": True, "message": "安装令牌已撤销"})


@router.get("/install")
@router.get("/install/info", include_in_schema=False)
async def install_settings(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    info = await install_info(request, session)
    values = await store().load(session)
    info["note"] = str(values.get("install_script_note") or "")
    return stamp(info)


@public_router.get("/install.ps1", response_class=PlainTextResponse)
async def install_script(
    request: Request,
    token: str = Query(default=""),
    download: int = Query(default=0),
) -> PlainTextResponse:
    async with request.app.state.db.sessionmaker() as session:
        credential = await require_install_credential(request, session, token=token)
    script = render_ps1(request, credential)
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="Observatory-Install-{resolve_version()}.ps1"'
        )
    return PlainTextResponse(script, media_type="text/plain; charset=utf-8", headers=headers)


@public_router.get("/install.cmd", response_class=PlainTextResponse)
async def install_wrapper(
    request: Request,
    token: str = Query(default=""),
) -> PlainTextResponse:
    async with request.app.state.db.sessionmaker() as session:
        credential = await require_install_credential(request, session, token=token)
    wrapper = render_cmd(request, credential)
    return PlainTextResponse(
        wrapper,
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="Observatory-Install-{resolve_version()}.cmd"'
            ),
        },
    )


@public_router.get("/bootstrap")
async def bootstrap(request: Request) -> dict[str, Any]:
    settings = get_settings(request)
    return stamp(
        {
            "version": resolve_version(),
            "base_urls": list(settings.base_urls),
            "console_host": settings.console_hosts[0] if settings.console_hosts else "",
            "enroll_required": True,
            "install_token_required": True,
        }
    )


@public_router.get("/agent/latest")
async def latest_agent(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    packages = service(request)
    package = await packages.latest(session, "agent")
    if package is None:
        return stamp({"version": resolve_version(), "available": False, "server_version": resolve_version()})
    return stamp(
        {
            "version": package.version,
            "available": True,
            "sha256": package.sha256,
            "size_bytes": package.size_bytes,
            "url": packages.url_for(package),
            "server_version": resolve_version(),
        }
    )


@public_router.get("/agent/{version}/package")
async def download_agent(
    version: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    from ..core.config import parse_version

    try:
        parse_version(version)
    except ValueError as exc:
        raise AppError.bad_request("版本号格式错误") from exc
    packages = service(request)
    package = next((item for item in await packages.list(session, "agent") if item.version == version), None)
    if package is None:
        raise AppError.gone(f"没有 {version} 版本的安装包")
    path = packages.file_for(package)
    if not path.exists():
        raise AppError.gone("安装包文件已丢失")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=package.filename,
        headers={
            "X-Sha256": package.sha256,
            "X-Version": package.version,
            "Cache-Control": "no-store",
        },
    )


__all__ = [
    "DEFAULT_TOKEN_TTL_S",
    "MAX_TOKEN_USES",
    "install_info",
    "public_router",
    "render_cmd",
    "render_ps1",
    "require_install_credential",
    "router",
    "validate_install_token",
]