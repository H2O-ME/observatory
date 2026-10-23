"""媒体层装配门面：帧摄取 → 转码 → 落盘 → 保留 → 归档。

对外只暴露三件事（其余模块仍可直接 import）：

* :func:`configure`：装配期注入静态配置与会话工厂（归档 worker 需要自己的会话）；
* :func:`start` / :func:`stop`：归档 worker 的生命周期（lifespan 调用一次）；
* :data:`ingest` / :data:`retention` / :data:`archive`：三个被其他层直接调用的模块。

本层不 import ``legacy``、不 import 旧 ``services``；正向依赖只有 ``core``（配置、
时钟、模型、数据库会话）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from . import archive, ingest, retention, storage, transcode
from .settings import MediaSettings, configure as configure_settings, load, resolve

log = logging.getLogger(__name__)

__all__ = [
    "MediaSettings",
    "archive",
    "configure",
    "ingest",
    "load",
    "resolve",
    "retention",
    "run_retention",
    "start",
    "stop",
    "storage",
    "transcode",
]

_session_scope: Callable[[], Any] | None = None


def configure(
    *,
    static: Any | None = None,
    session_scope: Callable[[], Any] | None = None,
    client_factory: Callable[[], Any] | None = None,
    clock: Callable[[], int] | None = None,
    api_base: str | None = None,
) -> None:
    """装配期注入（幂等）：静态配置、归档 worker 的会话/HTTP 工厂、时钟与 API 根地址。

    ``session_scope`` 需要返回一个异步上下文管理器：``async with session_scope() as s``。
    ``clock``/``api_base`` 主要给端到端测试用（可控时间、指向本地假 GitHub）；
    生产留空即使用系统时钟与 ``settings.archive_api_base``。
    """

    global _session_scope
    previous = archive.get_service()
    if previous.stats().get("worker_running"):
        # 装配期本不该重复 configure；真发生了说明调用方漏了 stop，记一条以免静默泄漏 worker。
        log.warning("media.configure 覆盖了一个仍在运行的归档 worker（请先 await media.stop()）")
    configure_settings(static)
    if session_scope is not None:
        _session_scope = session_scope
    options: dict[str, Any] = {"session_scope": _session_scope}
    if client_factory is not None:
        options["client_factory"] = client_factory
    if clock is not None:
        options["clock"] = clock
    if api_base is not None:
        options["api_base"] = api_base
    archive.set_service(archive.ArchiveService(**options))


def ensure_dirs(settings: MediaSettings | None = None) -> None:
    """确保 frames/ thumbs/ 两棵树存在（启动时调用一次即可）。"""

    resolved = settings or resolve()
    for path in (resolved.data_dir, resolved.frames_dir, resolved.thumbs_dir):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("创建媒体目录失败 %s: %s", path, exc)


async def start(*, queue_size: int | None = None) -> None:
    """启动归档 worker（进程内唯一）。"""

    await archive.get_service().start(queue_size=queue_size)
    stats = archive.get_service().stats()
    log.info("媒体层已启动（归档 worker 运行中=%s）", stats["worker_running"])


async def stop() -> None:
    await archive.get_service().stop()


async def run_retention(session: Any, *, now_ms: int | None = None) -> dict[str, int]:
    """周期保留入口（housekeeping 可直接调用）。"""

    return await retention.run_once(session, now_ms=now_ms)