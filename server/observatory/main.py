"""Application assembly (spec §4.1). Deliberately thin: lifespan, middleware,
router mounting, health/version and the SPA fallback. Feature logic lives in
``link``, ``presence``, ``console`` and ``media``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

import sqlalchemy

from . import presence
from .adapters.hostsplit import HostSplit
from .core import clock, errors
from .core.config import PRODUCT, Settings, get_settings, parse_version, resolve_version
from .core.crypto import RateLimiter
from .core.db import Database
from .core.log import configure_logging, log_event
from .core.models import Package
from .link.carrier_http import router as link_http_router
from .link.carrier_ws import router as link_ws_router
from .link.journal import CommandJournal
from .link.registry import DatabaseDeviceDirectory, registry

log = logging.getLogger(__name__)

#: Optional feature packages: mounted when present, warning when not. Temporary
#: W0 mechanism — W3 turns this into a hard import (see tmp_w0/server-core.md).
OPTIONAL_PACKAGES = (("observatory.console", "console"),)

#: Media has no HTTP router (its API lives in console); it is wired via
#: ``media.configure/start/stop`` below, and retention runs every housekeeping tick.
MEDIA_RETENTION_EVERY = 1


def _load_media() -> Any | None:
    try:
        from . import media
    except ImportError as exc:
        log.warning("media package unavailable (%s): frame ingestion/retention disabled", exc)
        return None
    return media


async def _start_media(app: FastAPI, database: Database) -> None:
    media = _load_media()
    app.state.media = media
    if media is None:
        return
    try:
        media.configure(static=app.state.settings, session_scope=database.session)
        media.ensure_dirs(media.resolve(app.state.settings))
        await media.start()
        log.info("media layer started (archive worker running)")
    except Exception as exc:  # noqa: BLE001 - media must not prevent the API from serving
        log.exception("media layer failed to start (%s); continuing without frame storage", exc)
        app.state.media = None


async def _stop_media(app: FastAPI) -> None:
    media = getattr(app.state, "media", None)
    if media is None:
        return
    try:
        await media.stop()
    except Exception as exc:  # noqa: BLE001 - shutdown continues for the other layers
        log.exception("media layer failed to stop cleanly (%s)", exc)


async def _housekeeping(app: FastAPI) -> None:
    interval = app.state.settings.housekeeping_interval_s
    tick = 0
    while True:
        await asyncio.sleep(interval)
        tick += 1
        try:
            expired = await app.state.journal.expire_stale()
            if expired:
                log_event(log, logging.INFO, "housekeeping.commands_expired", count=expired)
            media = getattr(app.state, "media", None)
            if media is not None and tick % MEDIA_RETENTION_EVERY == 0:
                async with app.state.db.sessionmaker() as session:
                    stats = await media.run_retention(session)
                if stats:
                    log_event(log, logging.INFO, "housekeeping.media_retention", **{str(k): v for k, v in stats.items()})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - maintenance must never kill the loop
            log_event(log, logging.ERROR, "housekeeping.failed", error=str(exc))


def _mount_optional(app: FastAPI, module_name: str, label: str) -> None:
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        log.warning("optional %s package not available yet (%s): %s", label, module_name, exc)
        return
    router = getattr(module, "router", None)
    if router is None:
        log.warning("optional %s package %s exposes no `router`; feature disabled", label, module_name)
        return
    app.include_router(router, prefix="/api/v1")
    log.info("%s router mounted from %s", label, module_name)


def _mount_legacy_agent(app: FastAPI) -> None:
    """Mount the 0.2.x device protocol translator (N2 zero-reinstall upgrade)."""

    try:
        from .legacy.agent_v2 import router as legacy_agent_router
    except ImportError as exc:
        log.warning("legacy 0.2.x agent adapter unavailable (%s); old agents cannot connect", exc)
        return
    app.include_router(legacy_agent_router, prefix="/api/v1")
    log.info("legacy agent v2 router mounted at /api/v1/agent (enroll/heartbeat/commands/frames/ws)")


def _load_agent_config_provider() -> Any | None:
    """Console-backed provider for ``welcome.config`` (same function as its DTO).

    ``console/dto.device_safe_config`` delegates to ``core.settings_spec.agent_view``;
    link never imports console, so the finished dict is produced here and injected.
    """

    try:
        from .console.deps import store
        from .console.dto import device_safe_config
    except ImportError as exc:
        log.warning("console agent-config provider unavailable (%s); welcome.config uses safe defaults", exc)
        return None

    async def provider(session: Any, device_settings: dict[str, Any]) -> dict[str, Any]:
        return device_safe_config(await store().effective(session, device_settings))

    return provider


def _uptime_s(app: FastAPI) -> int:
    started = getattr(app.state, "started_at_ms", 0)
    return max(0, (clock.now_ms() - started) // 1000) if started else 0


async def _latest_agent_version(database: Database) -> str | None:
    """Newest uploaded agent package, feeding presence's `update` capability (A13)."""

    async with database.sessionmaker() as session:
        rows = (
            await session.execute(sqlalchemy.select(Package.version).where(Package.kind == "agent"))
        ).scalars().all()
    versions = [str(value) for value in rows if value]
    return max(versions, key=parse_version) if versions else None


async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    database = Database(settings)
    report = await database.migrate()
    app.state.db = database
    app.state.journal = CommandJournal(database.sessionmaker, settings=settings, clock=clock.SYSTEM_CLOCK)
    app.state.links = registry
    app.state.clock = clock.SYSTEM_CLOCK
    app.state.device_directory = DatabaseDeviceDirectory(database.sessionmaker, settings)
    app.state.started_at_ms = clock.now_ms()
    app.state.agent_config_provider = _load_agent_config_provider()
    directory_name = type(app.state.device_directory).__name__
    latest_agent_version = None
    try:
        latest_agent_version = await _latest_agent_version(database)
    except Exception as exc:  # noqa: BLE001 - a package read failure must not block serving
        log.warning("could not read latest agent package (%s); update capability disabled", exc)
    presence.configure(
        settings,
        registry,
        latest_agent_version=latest_agent_version,
        clock=clock.SYSTEM_CLOCK,
    )
    log_event(
        log,
        logging.INFO,
        "device_directory.ready",
        directory=directory_name,
        agent_config_provider="console" if app.state.agent_config_provider else "defaults",
    )
    await _start_media(app, database)
    task = asyncio.create_task(_housekeeping(app), name="observatory-housekeeping")
    log_event(
        log,
        logging.INFO,
        "app.started",
        version=resolve_version(),
        schema_version=report.to_version,
        device_directory=directory_name,
        agent_config="console" if app.state.agent_config_provider else "defaults",
    )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            # Reason: cancellation is the normal way to stop housekeeping.
            await task
        await _stop_media(app)
        closed = await registry.close_all()
        presence.reset()
        await database.close()
        log_event(log, logging.INFO, "app.stopped", closed_sessions=closed)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=PRODUCT,
        version=resolve_version(),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = None
    app.state.journal = None
    app.state.links = registry
    app.state.device_directory = None
    app.state.agent_config_provider = None
    app.state.link_limiter = RateLimiter(settings.link_rate_limit, settings.link_rate_window_s)

    app.add_middleware(
        HostSplit,
        console_hosts=settings.console_hosts,
        agent_hosts=settings.agent_host_list,
        trust_forwarded_for=settings.trust_forwarded_for,
        allow_loopback_console=settings.allow_loopback_console,
    )
    errors.install_error_handlers(app)

    app.include_router(link_http_router)
    app.include_router(link_ws_router)
    for module_name, label in OPTIONAL_PACKAGES:
        _mount_optional(app, module_name, label)
    # Must be before the SPA catch-all: 0.2.x agents POST/GET these paths.
    _mount_legacy_agent(app)

    @app.get("/api/v1/health")
    async def health() -> dict:
        return {
            "ok": True,
            "product": PRODUCT,
            "version": resolve_version(),
            "server_time_ms": clock.now_ms(),
            "uptime_s": _uptime_s(app),
            "carriers": registry.counts(),
        }

    @app.get("/api/v1/version")
    async def version() -> dict:
        return {
            "product": PRODUCT,
            "version": resolve_version(),
            "base_urls": list(settings.base_urls),
            "server_time_ms": clock.now_ms(),
        }

    webui_dir = Path(settings.webui_dir)
    assets_dir = webui_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        index_file = webui_dir / "index.html"
        if full_path.startswith("api"):
            return JSONResponse(
                {"code": "BAD_REQUEST", "message": "接口不存在", "retryable": False, "server_time_ms": clock.now_ms()},
                status_code=404,
            )
        if not index_file.exists():
            return JSONResponse(
                {
                    "code": "BAD_REQUEST",
                    "message": "控制台前端尚未构建",
                    "retryable": False,
                    "server_time_ms": clock.now_ms(),
                },
                status_code=404,
            )
        candidate = (webui_dir / full_path).resolve()
        try:
            candidate.relative_to(webui_dir.resolve())
        except ValueError as exc:
            raise errors.AppError.bad_request("路径非法") from exc
        if candidate.is_file():
            response = FileResponse(candidate)
            if full_path.startswith("assets/"):
                response.headers["cache-control"] = "public, max-age=31536000, immutable"
            return response
        return FileResponse(index_file, headers={"cache-control": "no-cache"})

    return app


app = create_app()


__all__ = ["app", "create_app", "lifespan"]