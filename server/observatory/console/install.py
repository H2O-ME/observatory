"""Installer artifacts and package downloads.

Registration is open (0.3.7 / P4): downloading the install script needs no
credential at all. The deployment ticket — the previous "who may install"
gate — was removed together with its bindings, and so were delete tombstones:
a device is identified by its own credential (``X-Device-Token``), admission is
automatic, and deleting a device only forgets its record.

What is left here is a *rendering* job plus the read-only package directory:

* ``GET /api/v1/public/install.ps1`` / ``.cmd`` → the templates with version and
  server addresses substituted;
* ``GET /api/v1/public/agent/latest`` / ``.../{version}/package`` → the agent
  package the installer downloads (sha256 is verified client-side).

Scripts are still IP-throttled: they are cheap to serve but they are public and
unauthenticated, so line-rate scraping gains nothing and costs us bandwidth.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.config import resolve_version
from ..core.db import get_session
from ..core.errors import AppError
from .deps import client_ip, get_settings, require_admin, store
from .dto import stamp
from .envelope import ConsoleRoute
from .updates import service

log = logging.getLogger(__name__)

router = APIRouter(tags=["install"], dependencies=[Depends(require_admin)], route_class=ConsoleRoute)
public_router = APIRouter(prefix="/public", tags=["public"])


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


def _throttle_public(request: Request, kind: str) -> None:
    """F19: per-IP limiter for the public installer endpoints.

    The scripts carry no secret any more, but they are unauthenticated public
    downloads — rate limiting keeps a scrape from turning into a bandwidth bill.
    """

    limiter = getattr(request.app.state, "install_limiter", None)
    if limiter is None:
        return
    key = f"public-install:{kind}:{client_ip(request)}"
    if not limiter.allow(key):
        raise AppError.rate(f"请求过于频繁，请 {limiter.retry_after(key)} 秒后再试")


def _substitute(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered


def render_ps1(request: Request) -> str:
    settings = get_settings(request)
    template = _read_template("install.ps1")
    quoted = ", ".join(f"'{url}'" for url in settings.base_urls)
    replacements = {
        "@@VERSION@@": resolve_version(),
        "@@BASE_URLS@@": quoted,
        "@@CONSOLE_HOST@@": settings.console_hosts[0] if settings.console_hosts else "",
        "@@SERVER_TIME_MS@@": str(now_ms()),
    }
    # Windows PowerShell 5.1 decodes a BOM-less .ps1 as ANSI; the download
    # workflow requires the BOM (the `irm | iex` form ignores it).
    return "\ufeff" + _substitute(template, replacements)


def render_cmd(request: Request) -> str:
    settings = get_settings(request)
    template = _read_template("install.cmd")
    url_lines = "\n".join(
        f'set "URL{index}={url}"' for index, url in enumerate(settings.base_urls, start=1)
    )
    replacements = {
        "@@VERSION@@": resolve_version(),
        "@@URL_LINES@@": url_lines,
    }
    return _substitute(template, replacements)


async def install_info(request: Request, session: AsyncSession | None = None) -> dict[str, Any]:
    """Installer metadata for the console install page.

    ``enabled`` stays in the payload for the existing WebUI, but it is always
    true now: open registration means the installer works as soon as the server
    is reachable, with nothing to mint first.
    """

    settings = get_settings(request)
    version = resolve_version()
    return {
        "enabled": True,
        "enroll_required": True,
        "version": version,
        "console_host": settings.console_hosts[0] if settings.console_hosts else "",
        "base_urls": list(settings.base_urls),
        "script_url": "/api/v1/public/install.ps1",
        "cmd_url": "/api/v1/public/install.cmd",
        "artifacts": [
            {
                "kind": "cmd",
                "label": "安装程序 (.cmd)",
                "filename": f"Observatory-Install-{version}.cmd",
                "url": "/api/v1/public/install.cmd",
                "hint": "双击运行；需要管理员权限一次，之后自动更新与远程卸载都不再弹 UAC",
            },
            {
                "kind": "ps1",
                "label": "安装脚本 (.ps1)",
                "filename": f"Observatory-Install-{version}.ps1",
                "url": "/api/v1/public/install.ps1?download=1",
                "hint": "批量部署/组策略用；也可直接 irm 管道执行",
            },
        ],
    }


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
async def install_script(request: Request, download: int = 0) -> PlainTextResponse:
    _throttle_public(request, "ps1")
    script = render_ps1(request)
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="Observatory-Install-{resolve_version()}.ps1"'
        )
    return PlainTextResponse(script, media_type="text/plain; charset=utf-8", headers=headers)


@public_router.get("/install.cmd", response_class=PlainTextResponse)
async def install_wrapper(request: Request) -> PlainTextResponse:
    _throttle_public(request, "cmd")
    wrapper = render_cmd(request)
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
) -> Response:
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
    headers = {
        "X-Sha256": package.sha256,
        "X-Version": package.version,
        "Cache-Control": "no-store",
        # 0.3.26：受控端下载带断点续传（0.3.26 起 agent 侧 Range 重试），
        # 必须声明并支持区间请求。
        "Accept-Ranges": "bytes",
    }
    file_size = path.stat().st_size
    kind, bounds = _parse_range(request.headers.get("range", ""), file_size)
    if kind == "invalid":
        # 单区间 bytes 语法但区间非法（含 start 越界）：416 + 真实区间
        # （RFC 9110 §14.2）。
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    disposition = f'attachment; filename="{package.filename}"'
    if kind == "range" and bounds is not None:
        start, end = bounds
        length = end - start + 1
        return StreamingResponse(
            _file_slice(path, start, length),
            media_type="application/zip",
            status_code=206,
            headers={
                **headers,
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
                "Content-Disposition": disposition,
            },
        )
    # 无 Range / 多区间 / 非 bytes 单位 / 解析不了：整包 200（不误伤老客户端）。
    # 这里不能用 FileResponse：新版 Starlette 的 FileResponse 会自己再解析一遍
    # Range（多区间回 206 multipart、语法错回 400），与本端点"忽略区间"的契约
    # 冲突，且 416 只发 ``*/size``（缺 bytes 前缀）。整条链自己出流，行为不随
    # Starlette 版本漂移。
    return StreamingResponse(
        _file_slice(path, 0, file_size),
        media_type="application/zip",
        status_code=200,
        headers={
            **headers,
            "Content-Length": str(file_size),
            "Content-Disposition": disposition,
        },
    )


def _parse_range(header: str, file_size: int) -> tuple[str, tuple[int, int] | None]:
    """解析 ``Range`` 头，返回 ``(kind, bounds)``：

    * ``("range", (start, end))`` — 单区间 bytes 且可服务（含后缀 ``-N`` 展开，
      ``end`` 已闭合并收敛到 ``file_size - 1``）；
    * ``("invalid", None)`` — 单区间 bytes 语法但区间非法（start 越界 / end <
      start）→ 调用方回 416；
    * ``("full", None)`` — 无 Range / 多区间 / 非 bytes 单位 / 语法解析不了 →
      调用方忽略区间回 200 整包（RFC 9110 允许服务器不支持时忽略）。
    """

    text = (header or "").strip().lower()
    if not text.startswith("bytes="):
        return "full", None
    spec = text[len("bytes=") :].strip()
    if "," in spec:
        # 多区间合法但不支持：忽略而不是 416。
        return "full", None
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    try:
        if not first:
            # 后缀形式 ``-N``：取最后 N 字节。
            suffix = int(last)
            if suffix <= 0 or file_size <= 0:
                return "invalid", None
            return "range", (max(0, file_size - suffix), file_size - 1)
        start = int(first)
        if start < 0 or start >= file_size:
            return "invalid", None
        if not last:
            return "range", (start, file_size - 1)
        end = int(last)
        if end < start:
            return "invalid", None
        return "range", (start, min(end, file_size - 1))
    except ValueError:
        return "full", None


def _file_slice(path: Path, start: int, length: int, *, chunk_size: int = 256 * 1024):
    """按区间产出文件字节；同步生成器由 Starlette 放线程池迭代，不堵事件循环。

    包文件一经发布不可变（``PackageService.save`` 走临时名 + replace），不存在
    读取中途被改写的竞争。
    """

    with open(path, "rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            data = handle.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


__all__ = [
    "install_info",
    "public_router",
    "render_cmd",
    "render_ps1",
    "router",
]
