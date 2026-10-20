"""Application factory.

One container, one app: TLS terminates here, SQLite lives on the data volume, and
the same process serves the console SPA, the device API and both WebSockets.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.staticfiles import StaticFiles

from .api.public import install_script as serve_install_script
from .config import Settings, get_settings
from .hostsplit import HostSplit
from .services import commands as command_service
from .services import events
from .services.runtime import Runtime, get_runtime, set_runtime
from .version import resolve_version

log = logging.getLogger(__name__)

HOUSEKEEPING_INTERVAL_S = 20
RETENTION_EVERY_TICKS = 30  # ~10 minutes

#: Folder baked into the image next to the ``observatory`` package.
BUNDLED_WEBUI = Path(__file__).resolve().parent.parent / "webui-dist"


def sync_webui(settings: Settings, version: str) -> None:
    """Publish the console build bundled with this image into the data volume.

    The console lives in the writable data dir so it can be replaced without
    rebuilding the container, but the image always knows the version it shipped.
    A marker file keeps the two from silently drifting apart after an upgrade.
    """

    source = BUNDLED_WEBUI
    if not (source / "index.html").exists():
        return
    target = Path(settings.webui_dir)
    marker = target / ".version"
    try:
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == version:
            return
    except OSError:
        pass

    log.info("publishing console %s from %s", version, source)
    staging = target.with_name(target.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(source, staging)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        staging.replace(target)
        (target / ".version").write_text(version + "\n", encoding="utf-8")
    except OSError as exc:
        log.error("could not publish console build: %s", exc)
        shutil.rmtree(staging, ignore_errors=True)


async def housekeeping(runtime: Runtime) -> None:
    """Periodic maintenance: staleness, command expiry, retention."""

    tick = 0
    while True:
        await asyncio.sleep(HOUSEKEEPING_INTERVAL_S)
        tick += 1
        try:
            removed = runtime.registry.purge_stale()
            if removed:
                log.info("dropped %d stale channel(s)", removed)
                await events.emit(runtime.console_hub, events.EVENT_DEVICE_UPDATED, data={"stale_channels": removed})
            async with runtime.db.sessionmaker() as session:
                expired = await command_service.expire_stale(session, runtime.bus, runtime.console_hub)
                if expired:
                    log.info("expired %d command(s)", expired)
            if tick % RETENTION_EVERY_TICKS == 0 and runtime.retention is not None:
                async with runtime.db.sessionmaker() as session:
                    await runtime.retention.run_once(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - maintenance must never kill the loop
            log.exception("housekeeping pass failed")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    sync_webui(settings, resolve_version())
    runtime = await Runtime.create(settings)
    set_runtime(runtime)
    app.state.runtime = runtime
    task = asyncio.create_task(housekeeping(runtime), name="observatory-housekeeping")
    log.info("Observatory %s started (data_dir=%s)", runtime.version, settings.data_dir)
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await runtime.shutdown()
        set_runtime(None)
        log.info("Observatory stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()

    app = FastAPI(
        title="Observatory",
        version=resolve_version(),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.runtime = None

    app.add_middleware(
        HostSplit,
        console_hosts=settings.console_hosts,
        agent_hosts=settings.agent_host_list,
    )

    @app.middleware("http")
    async def add_version_header(request: Request, call_next):
        response = await call_next(request)
        runtime = getattr(request.app.state, "runtime", None)
        if runtime is not None:
            response.headers.setdefault("X-Observatory-Version", runtime.version)
        return response

    from .api import api_router

    app.include_router(api_router, prefix="/api/v1")

    @app.get("/install.ps1", response_class=PlainTextResponse)
    async def install_root(code: str = "") -> PlainTextResponse:
        return await serve_install_script(code=code, runtime=get_runtime())

    webui_dir = Path(settings.webui_dir)
    assets = webui_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/")
    async def index():
        index_file = webui_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file, headers={"Cache-Control": "no-cache"})
        return JSONResponse(
            {
                "product": "Observatory",
                "version": get_runtime().version,
                "note": "控制台前端尚未构建（webui/dist 为空），API 可用",
                "api": "/api/v1/health",
            }
        )

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith(("api/", "api")):
            return JSONResponse({"detail": "接口不存在"}, status_code=404)
        index_file = webui_dir / "index.html"
        if not index_file.exists():
            return JSONResponse({"detail": "前端未构建"}, status_code=404)
        candidate = (webui_dir / full_path).resolve()
        try:
            candidate.relative_to(webui_dir.resolve())
        except ValueError:
            return JSONResponse({"detail": "路径非法"}, status_code=400)
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index_file, headers={"Cache-Control": "no-cache"})

    return app


app = create_app()


__all__ = ["app", "create_app", "housekeeping", "lifespan"]