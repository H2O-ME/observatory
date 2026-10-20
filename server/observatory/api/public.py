"""Public (unauthenticated) endpoints: version, health, installer, packages.

Served on **both** domains so a machine that cannot resolve or reach one of them
can still install and update. Nothing here returns fleet data.
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_session
from ..models import now_ms
from ..schemas import VersionOut
from ..services import installer, storage
from ..services.runtime import Runtime, get_runtime
from ..version import parse_version

log = logging.getLogger(__name__)
router = APIRouter(tags=["public"])
public_router = APIRouter(prefix="/public", tags=["public"])


def _require_code(runtime: Runtime, code: str) -> str:
    expected = runtime.settings.enroll_code
    if not expected:
        raise HTTPException(status_code=403, detail="服务器未开启注册")
    if not code or not hmac.compare_digest(code.strip(), expected):
        raise HTTPException(status_code=403, detail="注册码不正确")
    return code.strip()


def _attachment(name: str) -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="{name}"',
    }


@router.get("/health")
async def health(runtime: Runtime = Depends(get_runtime)) -> dict:
    return {
        "ok": True,
        "version": runtime.version,
        "uptime_s": runtime.uptime_s(),
        "channels": runtime.registry.counts(),
        "server_time_ms": now_ms(),
    }


@router.get("/version", response_model=VersionOut)
async def version(runtime: Runtime = Depends(get_runtime)) -> VersionOut:
    return VersionOut(version=runtime.version, server_time_ms=now_ms())


def render_install_script(runtime: Runtime, code: str) -> str:
    """Backwards-compatible alias used by main.py's ``/install.ps1`` route."""

    return installer.render_ps1(runtime, code)


@public_router.get("/install.ps1", response_class=PlainTextResponse)
async def install_script(
    code: str = Query(default="", description="注册码，与控制台一致"),
    download: int = Query(default=0, description="传 1 则作为附件下载"),
    runtime: Runtime = Depends(get_runtime),
) -> PlainTextResponse:
    script = installer.render_ps1(runtime, _require_code(runtime, code))
    headers = {"Cache-Control": "no-store"}
    if download:
        headers.update(_attachment(f"Observatory-Install-{runtime.version}.ps1"))
    return PlainTextResponse(script, media_type="text/plain; charset=utf-8", headers=headers)


@public_router.get("/install.cmd", response_class=PlainTextResponse)
async def install_wrapper(
    code: str = Query(default="", description="注册码，与控制台一致"),
    runtime: Runtime = Depends(get_runtime),
) -> PlainTextResponse:
    """Double-clickable installer: enroll code and failover list already inside."""

    wrapper = installer.render_cmd(runtime, _require_code(runtime, code))
    return PlainTextResponse(
        wrapper,
        media_type="text/plain; charset=utf-8",
        headers=_attachment(f"Observatory-Install-{runtime.version}.cmd"),
    )


@public_router.get("/agent/latest")
async def latest_agent(
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    package = await runtime.packages.latest(session, "agent") if runtime.packages else None
    if package is None:
        return {
            "version": runtime.version,
            "available": False,
            "server_version": runtime.version,
            "server_time_ms": now_ms(),
        }
    return {
        "version": package.version,
        "available": True,
        "sha256": package.sha256,
        "size_bytes": package.size_bytes,
        "url": runtime.packages.url_for(package),
        "server_version": runtime.version,
        "server_time_ms": now_ms(),
    }


@public_router.get("/agent/{version}/package")
async def download_agent(
    version: str,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> FileResponse:
    try:
        parse_version(version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="版本号格式错误") from exc
    if runtime.packages is None:
        raise HTTPException(status_code=503, detail="安装包服务未就绪")
    packages = await runtime.packages.list(session, "agent")
    package = next((item for item in packages if item.version == version), None)
    if package is None:
        raise HTTPException(status_code=404, detail=f"没有 {version} 版本的安装包")
    path = runtime.packages.file_for(package)
    if not path.exists():
        raise HTTPException(status_code=410, detail="安装包文件已丢失")
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


@public_router.get("/bootstrap")
async def bootstrap(runtime: Runtime = Depends(get_runtime)) -> dict:
    """What an installer needs to know before it has a device token."""

    return {
        "version": runtime.version,
        "base_urls": runtime.settings.base_urls,
        "console_host": runtime.settings.console_hosts[0] if runtime.settings.console_hosts else "",
        "enroll_required": bool(runtime.settings.enroll_code),
        "server_time_ms": now_ms(),
    }


_ = storage

__all__ = ["public_router", "render_install_script", "router"]