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
from ..services import storage
from ..services.runtime import Runtime, get_runtime
from ..version import parse_version

log = logging.getLogger(__name__)
router = APIRouter(tags=["public"])
public_router = APIRouter(prefix="/public", tags=["public"])

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "install.ps1"


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
    """Fill the installer template.

    The enrollment code is injected per request and never baked into the file on
    disk, so rotating it only requires changing ``OBS_ENROLL_CODE``.
    """

    try:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - packaging error
        log.error("installer template missing: %s", exc)
        raise HTTPException(status_code=500, detail="安装脚本模板缺失") from exc

    base_urls = runtime.settings.base_urls
    quoted = ", ".join(f"'{url}'" for url in base_urls)
    replacements = {
        "@@VERSION@@": runtime.version,
        "@@ENROLL_CODE@@": code,
        "@@BASE_URLS@@": quoted,
        "@@CONSOLE_HOST@@": runtime.settings.console_hosts[0] if runtime.settings.console_hosts else "",
        "@@SERVER_TIME_MS@@": str(now_ms()),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


@public_router.get("/install.ps1", response_class=PlainTextResponse)
async def install_script(
    code: str = Query(default="", description="注册码，与控制台一致"),
    runtime: Runtime = Depends(get_runtime),
) -> PlainTextResponse:
    expected = runtime.settings.enroll_code
    if not expected:
        raise HTTPException(status_code=403, detail="服务器未开启注册")
    if not code or not hmac.compare_digest(code.strip(), expected):
        raise HTTPException(status_code=403, detail="注册码不正确")
    script = render_install_script(runtime, code.strip())
    return PlainTextResponse(
        script,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
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