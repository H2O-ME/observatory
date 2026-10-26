"""Application assembly (spec §4.1). Deliberately thin: lifespan, middleware,
router mounting, health/version and the SPA fallback. Feature logic lives in
``link``, ``presence``, ``console`` and ``media``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
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
from .core.settings_spec import SPECS
from .link.carrier_http import router as link_http_router
from .link.carrier_ws import router as link_ws_router
from .link.journal import CommandJournal
from .link.registry import DatabaseDeviceDirectory, registry

log = logging.getLogger(__name__)

#: 强引用在途的后台 fire-and-forget 任务（见 frame-event 推送处注释）。
_BACKGROUND_TASKS: set[asyncio.Task] = set()

#: Media has no HTTP router (its API lives in console); it is wired via
#: ``media.configure/start/stop`` below. Retention is expensive (unlink per frame),
#: so it runs on its own low-frequency schedule, not on every housekeeping tick.
MEDIA_RETENTION_KEY = "media_retention_interval_s"

#: WebUI resolution: the data volume wins, then the copy baked into the image
#: (``/srv/webui-dist`` in the Dockerfile), then — only for local development —
#: the repository build output. ``OBS_WEBUI_DIR`` is the explicit override.
BUNDLED_WEBUI = Path(__file__).resolve().parent.parent / "webui-dist"
REPO_WEBUI_DIST = Path(__file__).resolve().parents[2] / "webui" / "dist"
WEBUI_DEV_ENVS = frozenset({"dev", "development", "local"})


def _bundled_webui() -> Path | None:
    """The frontend baked into the image (or the repo build during development)."""

    if (BUNDLED_WEBUI / "index.html").is_file():
        return BUNDLED_WEBUI
    env = os.environ.get("OBS_ENV", "").strip().lower()
    if env in WEBUI_DEV_ENVS and (REPO_WEBUI_DIST / "index.html").is_file():
        return REPO_WEBUI_DIST
    return None


def _resolve_webui_dir(settings: Settings) -> tuple[Path, str]:
    """Return ``(directory, source)`` for the console assets (startup log uses it).

    镜像内置前端永远优先；数据卷副本仅作显式覆盖（OBS_WEBUI_DIR）之外的开发
    兜底。前后端一体镜像下不再存在"旧 SPA 配新 API"的可能。
    """

    override = os.environ.get("OBS_WEBUI_DIR", "").strip()
    if override:
        return Path(override).expanduser(), "OBS_WEBUI_DIR"
    # 前后端一体镜像（0.3.7 末重构）：镜像内的前端与本次 API 绑定同源，
    # 永远优先——数据卷副本不再参与生产路径（/data/webui/.version 机制废除）。
    bundled = _bundled_webui()
    if bundled is not None:
        return bundled, "bundled"
    configured = Path(settings.webui_dir)
    if (configured / "index.html").is_file():
        return configured, "data"
    return configured, "missing"



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


async def _media_retention_interval_s(app: FastAPI) -> int:
    """Effective retention cadence from runtime settings, clamped by its Spec."""

    spec = SPECS[MEDIA_RETENTION_KEY]
    value: Any = spec.default
    provider = getattr(app.state, "runtime_settings_provider", None)
    database = getattr(app.state, "db", None)
    if provider is not None and database is not None:
        try:
            async with database.sessionmaker() as session:
                runtime = await provider(session)
            value = runtime.get(MEDIA_RETENTION_KEY, value)
        except Exception as exc:  # noqa: BLE001 - fall back to the default cadence
            log_event(log, logging.WARNING, "housekeeping.runtime_settings_failed", error=str(exc))
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = int(spec.default)
    minimum = int(spec.minimum if spec.minimum is not None else interval)
    maximum = int(spec.maximum if spec.maximum is not None else interval)
    return max(minimum, min(interval, maximum))


async def _run_media_retention(app: FastAPI) -> None:
    """One retention pass in a background task; logs stats + wall time."""

    media = getattr(app.state, "media", None)
    database = getattr(app.state, "db", None)
    if media is None or database is None:
        return
    started = time.monotonic()
    try:
        async with database.sessionmaker() as session:
            stats = await media.run_retention(session) or {}
        duration_ms = int((time.monotonic() - started) * 1000)
        removed = sum(
            int(stats.get(key, 0) or 0)
            for key in ("frames", "dangling", "quota_reclaimed", "archives", "audit", "commands")
        )
        log_event(
            log,
            logging.INFO,
            "housekeeping.media_retention",
            frames=int(stats.get("frames", 0) or 0),
            frames_bytes=int(stats.get("frames_bytes", 0) or 0),
            removed=removed,
            orphans=int(stats.get("orphans", 0) or 0),
            duration_ms=duration_ms,
            dangling=int(stats.get("dangling", 0) or 0),
            quota_reclaimed=int(stats.get("quota_reclaimed", 0) or 0),
            audit=int(stats.get("audit", 0) or 0),
            commands=int(stats.get("commands", 0) or 0),
            archives=int(stats.get("archives", 0) or 0),
            archive_enqueued=int(stats.get("archive_enqueued", 0) or 0),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - maintenance must never kill the loop
        duration_ms = int((time.monotonic() - started) * 1000)
        log_event(
            log,
            logging.ERROR,
            "housekeeping.media_retention_failed",
            duration_ms=duration_ms,
            error=str(exc),
        )
    finally:
        app.state.retention_task = None


async def _housekeeping(app: FastAPI) -> None:
    interval = app.state.settings.housekeeping_interval_s
    app.state.retention_task = None
    app.state.last_retention_at = 0.0
    while True:
        await asyncio.sleep(interval)
        try:
            expired = await app.state.journal.expire_stale()
            if expired:
                log_event(log, logging.INFO, "housekeeping.commands_expired", count=expired)
            media = getattr(app.state, "media", None)
            if media is not None:
                task = app.state.retention_task
                if task is not None and task.done():
                    app.state.retention_task = None
                    task = None
                # Only refresh the cadence while retention is idle: a long pass
                # holds the DB, and a settings read here must not wait behind it.
                if task is None:
                    app.state.media_retention_interval_s = await _media_retention_interval_s(app)
                retention_interval_s = app.state.media_retention_interval_s
                now = time.monotonic()
                if task is None and now - app.state.last_retention_at >= retention_interval_s:
                    app.state.last_retention_at = now
                    app.state.retention_task = asyncio.create_task(
                        _run_media_retention(app),
                        name="observatory-media-retention",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - maintenance must never kill the loop
            log_event(log, logging.ERROR, "housekeeping.failed", error=str(exc))


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


def _load_runtime_settings_provider() -> Any | None:
    """Console-backed reader for runtime settings scheduling (e.g. retention)."""

    try:
        from .console.deps import store
    except ImportError as exc:
        log.warning("console runtime-settings provider unavailable (%s); using spec defaults", exc)
        return None

    async def provider(session: Any) -> dict[str, Any]:
        return await store().load(session)

    return provider


def _make_update_offer_provider(app: FastAPI) -> Any | None:
    """v3 update loop (F1): turn ``agent_version`` into a package offer.

    Reads the package table per call (throttled to 1/min/device by the session)
    and keeps presence's ``update`` capability in step with CLI-registered
    packages, which cannot notify the running process themselves.
    """

    def provider(device_id: int, agent_version: str) -> Any:
        return _update_offer(app, int(device_id), str(agent_version or ""))

    return provider


async def _update_offer(app: FastAPI, device_id: int, agent_version: str) -> dict[str, Any] | None:
    from .console.updates import PackageService
    from .core.config import can_self_update, is_newer
    from .core.models import Device

    if not can_self_update(agent_version):
        return None
    database = getattr(app.state, "db", None)
    if database is None:
        return None
    async with database.sessionmaker() as session:
        device = await session.get(Device, device_id)
        if device is None:
            return None
        offer = await PackageService(app.state.settings).update_offer(session, device)
    if offer and is_newer(str(offer.get("version") or ""), agent_version or "0.0.0"):
        with contextlib.suppress(Exception):
            presence.set_latest_agent_version(str(offer.get("version") or ""))
    return offer


def _make_agent_event_sink(app: FastAPI) -> Any:
    """Route uplink events the link cannot persist itself (F2/F4)."""

    async def sink(kind: str, device_id: int, uid: str, body: dict[str, Any]) -> None:
        database = getattr(app.state, "db", None)
        if kind == "uninstall_ack":
            await _handle_uninstall_ack(app, int(device_id), uid)
        elif kind == "command_updated":
            from .console.events import EVENT_COMMAND_UPDATED, emit

            await emit(EVENT_COMMAND_UPDATED, device_uid=uid, data=body)
        elif kind == "agent_event":
            if database is None:
                return
            from .console import audit as console_audit

            name = str(body.get("event") or "agent_event")[:64]
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            async with database.sessionmaker() as session:
                await console_audit.record(
                    session,
                    action=f"agent.{name}"[:64],
                    actor="agent",
                    device_uid=uid,
                    detail=console_audit.redact(data) if hasattr(console_audit, "redact") else data,
                )

    return sink


async def _handle_uninstall_ack(app: FastAPI, device_id: int, uid: str) -> None:
    """Persist ``uninstalled_at_ms``, audit, drop the session and revoke grants."""

    from .console import audit as console_audit
    from .console.control import revoke_device_grants
    from .core.models import Device

    database = getattr(app.state, "db", None)
    if database is not None:
        async with database.sessionmaker() as session:
            device = await session.get(Device, device_id)
            if device is not None and not device.uninstalled_at_ms:
                device.uninstalled_at_ms = clock.now_ms()
                await session.commit()
            await console_audit.record(
                session,
                action=console_audit.ACTION_DEVICE_UNINSTALL,
                device=device,
                device_uid=uid,
                detail={"stage": "acked", "protocol": 3},
            )
    links = getattr(app.state, "links", None)
    if links is not None:
        await links.unregister(device_id)
    await revoke_device_grants(app, device_id, reason="device uninstalled")


def _wire_frame_events(app: FastAPI) -> None:
    """Frame listener → console event hub (F4): make ``frame_added`` real."""

    media = getattr(app.state, "media", None)
    if media is None:
        return

    def on_frame(device: Any, frame: Any) -> None:
        async def push() -> None:
            try:
                from .console.events import EVENT_FRAME_ADDED, emit

                await emit(
                    EVENT_FRAME_ADDED,
                    device_uid=str(getattr(device, "uid", "") or ""),
                    data={
                        "frame_id": str(getattr(frame, "id", "") or ""),
                        "ts_ms": int(getattr(frame, "ts_ms", 0) or 0),
                    },
                )
            except Exception as exc:  # noqa: BLE001 - events are best-effort
                log.debug("frame event emit failed: %s", exc)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # P1（2026-10 审查）：事件循环对运行中任务只持弱引用——裸 create_task
        # 的返回值被 GC 时未开始的 frame_added 推送会凭空消失。存入模块级
        # 集合，done 回调里移除（asyncio 文档的标准写法）。
        task = loop.create_task(push(), name="frame-event")
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    try:
        # add_frame_listener lives on media.ingest (the package module does not
        # re-export it) — the 0.3.1 CI run surfaced this as a warning, which
        # silently disabled frame_added events.
        media.ingest.add_frame_listener(on_frame)
    except Exception as exc:  # noqa: BLE001 - event wiring must not break media start
        log.warning("frame event listener not registered (%s)", exc)


def _make_late_result_listener(app: FastAPI) -> Any:
    """F6: audit when a real result arrives after the console already saw timeout."""

    async def listener(record: Any, status: str, result: Any, error: Any) -> None:
        database = getattr(app.state, "db", None)
        if database is None:
            return
        from .console import audit as console_audit

        async with database.sessionmaker() as session:
            await console_audit.record(
                session,
                action="command.late_result",
                actor="agent",
                device_uid="",
                detail={
                    "command_id": str(getattr(record, "id", "") or ""),
                    "type": str(getattr(record, "kind", "") or ""),
                    "row_status": "timeout",
                    "late_status": status,
                    "error": str(error or "")[:200] or None,
                },
            )

    return listener


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


def _ensure_production_credentials(settings: Settings) -> None:
    """F14: refuse to boot with the documented default credentials (fail-closed).

    Only a non-dev environment is rejected: ``OBS_ENV=dev`` keeps the README
    quick-start working, everything else must set real values in ``server/.env``.
    """

    env = os.environ.get("OBS_ENV", "").strip().lower()
    if env in WEBUI_DEV_ENVS:
        return
    defaults = []
    if settings.secret_key == "change-me-in-production":
        defaults.append("OBS_SECRET_KEY")
    if settings.admin_password == "change-me-in-production":
        defaults.append("OBS_ADMIN_PASSWORD")
    if defaults:
        raise RuntimeError(
            "拒绝启动：以下凭据仍是文档默认值 "
            f"{', '.join(defaults)}。请在 server/.env 设置随机密钥后重启"
            "（开发环境可设 OBS_ENV=dev 跳过此检查）。"
        )


async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    _ensure_production_credentials(settings)
    database = Database(settings)
    report = await database.migrate()
    app.state.db = database
    app.state.journal = CommandJournal(
        database.sessionmaker,
        settings=settings,
        clock=clock.SYSTEM_CLOCK,
        late_result_listener=_make_late_result_listener(app),
    )
    app.state.links = registry
    app.state.clock = clock.SYSTEM_CLOCK
    app.state.device_directory = DatabaseDeviceDirectory(database.sessionmaker, settings)
    app.state.started_at_ms = clock.now_ms()
    app.state.agent_config_provider = _load_agent_config_provider()
    app.state.runtime_settings_provider = _load_runtime_settings_provider()
    app.state.update_offer_provider = _make_update_offer_provider(app)
    app.state.agent_event_sink = _make_agent_event_sink(app)
    async with database.sessionmaker() as session:
        from .console.auth import load_credential_version

        app.state.credential_version = await load_credential_version(session)
    app.state.retention_task = None
    app.state.media_retention_interval_s = int(SPECS[MEDIA_RETENTION_KEY].default)
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
    _wire_frame_events(app)
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
        retention_task = getattr(app.state, "retention_task", None)
        if retention_task is not None and not retention_task.done():
            retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                # Reason: a retention pass may be mid-unlink; cancel before DB close.
                await retention_task
        app.state.retention_task = None
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
    app.state.runtime_settings_provider = None
    app.state.retention_task = None
    app.state.media_retention_interval_s = int(SPECS[MEDIA_RETENTION_KEY].default)
    app.state.link_limiter = RateLimiter(settings.link_rate_limit, settings.link_rate_window_s)
    app.state.link_ip_limiter = RateLimiter(settings.link_ip_rate_limit, settings.link_rate_window_s)
    app.state.enroll_limiter = RateLimiter(settings.enroll_rate_limit, settings.enroll_rate_window_s)
    app.state.install_limiter = RateLimiter(settings.enroll_rate_limit, settings.enroll_rate_window_s)

    app.add_middleware(
        HostSplit,
        console_hosts=settings.console_hosts,
        agent_hosts=settings.agent_host_list,
        trust_forwarded_for=settings.trust_forwarded_for,
        allow_loopback_console=settings.allow_loopback_console,
        # 与 uvicorn --forwarded-allow-ips 同源：受信反代（宿主 nginx 走 docker
        # 网桥）转发的 loopback Host 视同本机流量（2026-10-26 生产事故复盘）。
        trusted_proxy_ips=os.environ.get(
            "OBS_FORWARDED_ALLOW_IPS", "127.0.0.1,172.16.0.0/12"
        ),
    )
    # P1（2026-10 审查）：网络层请求体上限——在 FastAPI 解析 body 之前拒绝
    # 超限请求（限流此前发生在 body 完整读入内存之后）。装在 HostSplit 内层：
    # 外域先 404，不向扫描器暴露 413 差异。
    from .adapters.bodylimit import BodyLimit  # noqa: PLC0415 - 就地导入保持启动顺序清晰

    app.add_middleware(
        BodyLimit,
        frame_limit=int(settings.max_frame_bytes),
    )
    errors.install_error_handlers(app)

    app.include_router(link_http_router)
    app.include_router(link_ws_router)
    from .console import console_router  # noqa: PLC0415 - hard dependency since W3

    app.include_router(console_router, prefix="/api/v1")

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

    webui_dir, webui_source = _resolve_webui_dir(settings)
    if webui_source == "missing":
        log.warning(
            "webui.index missing path=%s bundled=%s: set OBS_ENV=dev (repo webui/dist) "
            "or OBS_WEBUI_DIR, otherwise / returns 404",
            webui_dir,
            BUNDLED_WEBUI,
        )
    else:
        log.info("webui.serving dir=%s source=%s", webui_dir, webui_source)
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