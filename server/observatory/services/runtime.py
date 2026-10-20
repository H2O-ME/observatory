"""Process-wide runtime state.

One object owns every long-lived component so tests can build a server, exercise
it and tear it down without leaking background tasks, and so nothing reaches for
module globals in an ad-hoc way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..db import Database
from ..models import now_ms
from ..security import RateLimiter
from ..version import resolve_version
from .archive import ArchiveService
from .bus import CommandBus
from .control import ControlSessions
from .hub import AgentHub, ConsoleHub
from .notify import DeviceNotifier
from .packages import PackageService
from .presence import ChannelRegistry
from .retention import RetentionService
from .settings_store import SettingsStore
from .transcode import Thumbnailer

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    db: Database
    version: str = field(default_factory=resolve_version)
    started_at_ms: int = field(default_factory=now_ms)

    registry: ChannelRegistry = field(default_factory=ChannelRegistry)
    bus: CommandBus = field(default_factory=CommandBus)
    notifier: DeviceNotifier = field(default_factory=DeviceNotifier)
    agent_hub: AgentHub = field(default_factory=AgentHub)
    console_hub: ConsoleHub = field(default_factory=ConsoleHub)
    control: ControlSessions = field(default_factory=ControlSessions)

    store: SettingsStore = field(default_factory=SettingsStore)
    packages: PackageService | None = None
    thumbnailer: Thumbnailer = field(default_factory=Thumbnailer)
    retention: RetentionService | None = None

    login_limiter: RateLimiter | None = None
    enroll_limiter: RateLimiter | None = None
    #: Optional remote archive backend; inert unless ``archive_enabled`` is set.
    archive: Any = None

    @classmethod
    async def create(cls, settings: Settings | None = None) -> "Runtime":
        settings = settings or get_settings()
        settings.ensure_dirs()
        db = Database(settings)
        await db.create_all()
        runtime = cls(
            settings=settings,
            db=db,
            packages=PackageService(settings),
            retention=RetentionService(settings, SettingsStore()),
            archive=ArchiveService(settings),
            login_limiter=RateLimiter(settings.login_rate_limit, settings.login_rate_window_s),
            enroll_limiter=RateLimiter(settings.enroll_rate_limit, settings.enroll_rate_window_s),
        )
        # The retention service and the API must share one settings cache, or a
        # change made in the console would not be seen by the sweeper.
        runtime.retention.store = runtime.store
        runtime.agent_hub.notifier = runtime.notifier
        async with db.sessionmaker() as session:
            discovered = await runtime.packages.discover(session)
            if discovered:
                log.info("registered %d agent package(s) from disk", discovered)
        return runtime

    async def shutdown(self) -> None:
        self.bus.clear()
        self.registry.clear()
        self.notifier.clear()
        self.agent_hub.clear()
        self.console_hub.clear()
        try:
            await self.db.close()
        except Exception:  # noqa: BLE001 - disposing during shutdown may race the loop
            log.debug("database dispose failed during shutdown", exc_info=True)

    def server_time_ms(self) -> int:
        return now_ms()

    def uptime_s(self) -> int:
        return max(0, (now_ms() - self.started_at_ms) // 1000)


_runtime: Runtime | None = None


def set_runtime(runtime: Runtime | None) -> None:
    global _runtime
    _runtime = runtime


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("runtime is not initialised")
    return _runtime


def runtime_or_none() -> Runtime | None:
    return _runtime


__all__ = ["Runtime", "get_runtime", "runtime_or_none", "set_runtime"]