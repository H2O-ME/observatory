"""媒体层配置归一（唯一入口）。

为什么要多这一层：数据目录与硬上限来自静态配置（环境变量），而画质/保留/归档
开关是运行时配置（``settings`` 表）。两者必须**先归一成一个不可变对象**再传给
ingest / transcode / retention / archive，否则各层各自解释键名与默认值，就会重演
0.2.10 的口径漂移（配额按磁盘算 ``used``、删除却只删数据库行）。

本模块分两半：

* :func:`resolve` 是**纯函数**（无 IO），可被单测直接调用；
* :func:`load` 是唯一 IO 入口（读 ``settings`` 表，失败则退回默认值并记日志）。

键名与 ``settings_store.SPECS`` 保持一致；同一语义的旧键（如 ``jpeg_quality``）
作为别名接受，避免调用方为了本层改键名。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

DAY_MS = 86_400_000

#: GitHub API 根地址。单独成项而不是硬编码，是为了让端到端测试指向本地假 GitHub，
#: 也顺带支持 GitHub Enterprise 自建域名（0.2.10 把地址写死在 archive.py 里）。
GITHUB_API_DEFAULT = "https://api.github.com"

_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class MediaSettings:
    """媒体层需要的一切开关，一次解析、全程只读。"""

    # ---- 落盘位置（静态配置，来自 core.config.Settings） --------------------
    data_dir: Path
    frames_dir: Path
    thumbs_dir: Path
    max_frame_bytes: int = 6 * 1024 * 1024

    # ---- 转码 -------------------------------------------------------------
    max_width: int = 1600
    #: 服务端 WebP 质量。历史键名是 ``jpeg_quality``（受控端 JPEG 与服务端 WebP
    #: 共用同一个旋钮），0.3.0 保持键名与默认值 72 不变，避免破坏已下发的设备设置。
    webp_quality: int = 72
    thumb_width: int = 320
    thumb_quality: int = 68
    transcode_concurrency: int = 2

    # ---- 保留 -------------------------------------------------------------
    frame_retention_days: int = 14
    audit_retention_days: int = 180
    command_retention_days: int = 30
    storage_quota_mb: int = 4096
    quota_target: int = 95
    orphan_grace_ms: int = 15 * 60 * 1000

    # ---- 归档（GitHub 仓库） ----------------------------------------------
    archive_enabled: bool = False
    archive_repo: str = ""
    archive_token: str = ""
    archive_branch: str = "main"
    archive_path_prefix: str = "frames"
    #: GitHub API 根地址（e2e 用本地假 GitHub；自建实例同理）。
    archive_api_base: str = GITHUB_API_DEFAULT
    archive_queue_size: int = 256
    archive_max_attempts: int = 8
    archive_retry_base_s: int = 15
    archive_retry_cap_s: int = 3600
    archive_verify_readback: bool = True
    archive_remote_delete: bool = True
    # 归档感知保留：远端上传成功（或超出宽限期）之前不允许删本地。
    archive_gate_retention: bool = True
    archive_gate_grace_days: int = 7

    @property
    def quota_bytes(self) -> int:
        return max(0, int(self.storage_quota_mb)) * 1024 * 1024

    @property
    def quota_target_bytes(self) -> int:
        return int(self.quota_bytes * max(0, min(100, int(self.quota_target))) / 100)

    @property
    def archive_ready(self) -> bool:
        """归档是否真的可执行（开关 + 仓库 + 令牌三者齐备）。"""

        return bool(self.archive_enabled and self.archive_repo.strip() and self.archive_token.strip())

    def with_overrides(self, **changes: Any) -> "MediaSettings":
        return replace(self, **changes)


_STATIC: Any | None = None
_DEFAULTS: MediaSettings | None = None


def _fallback_static() -> Any:
    """没有 core.config 时的兜底：只保证数据目录存在，且明确告警。"""

    data_dir = Path(os.environ.get("OBS_DATA_DIR") or "data")
    log.warning("core.config 不可用，媒体层退回 OBS_DATA_DIR/%s 作为数据目录", data_dir)
    return type("FallbackConfig", (), {"data_dir": data_dir, "max_frame_bytes": 6 * 1024 * 1024})()


def static_config() -> Any:
    """返回静态配置对象（``core.config.Settings``），进程内缓存。"""

    global _STATIC
    if _STATIC is not None:
        return _STATIC
    try:
        from observatory.core.config import get_settings

        _STATIC = get_settings()
    except (ImportError, AttributeError) as exc:  # 只吞"模块未就绪"，其余照常抛出
        log.warning("读取 core.config.get_settings 失败（%s），使用环境变量兜底", exc)
        _STATIC = _fallback_static()
    return _STATIC


def configure(static: Any | None = None) -> None:
    """装配期注入静态配置（测试也用它替换数据目录）。"""

    global _STATIC, _DEFAULTS
    _STATIC = static
    _DEFAULTS = None


_LOAD_CACHE: dict[str, Any] = {}
_LOAD_CACHE_TTL_S = 30.0


def reset_cache() -> None:
    global _STATIC, _DEFAULTS
    _STATIC = None
    _DEFAULTS = None
    _LOAD_CACHE.clear()


# --------------------------------------------------------------------------
# 纯归一函数
# --------------------------------------------------------------------------
def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    return default


def _as_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        # 非数字值按默认值处理：范围/类型校验是 settings_store 的职责，本层只做归一，
        # 不为一个坏配置项阻断摄取/保留（否则旧库里的脏值会让整条存储链停摆）。
        return default
    if minimum is not None and number < minimum:
        return minimum
    if maximum is not None and number > maximum:
        return maximum
    return number


def _as_text(value: Any, default: str) -> str:
    if value is None:
        return default
    text_value = str(value).strip()
    return text_value if text_value else default


def _as_path(value: Any, default: Path) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value.strip())
    return default


def defaults() -> MediaSettings:
    """静态配置推导出的默认媒体配置（进程内缓存）。"""

    global _DEFAULTS
    if _DEFAULTS is not None:
        return _DEFAULTS
    static = static_config()
    data_dir = _as_path(getattr(static, "data_dir", None), Path("data"))
    frames_dir = _as_path(getattr(static, "frames_dir", None), data_dir / "frames")
    thumbs_dir = _as_path(getattr(static, "thumbs_dir", None), data_dir / "thumbs")
    _DEFAULTS = MediaSettings(
        data_dir=data_dir,
        frames_dir=frames_dir,
        thumbs_dir=thumbs_dir,
        max_frame_bytes=_as_int(getattr(static, "max_frame_bytes", None), 6 * 1024 * 1024, minimum=64 * 1024),
        archive_api_base=_as_text(
            getattr(static, "archive_api_base", None) or os.environ.get("OBS_ARCHIVE_API_BASE"),
            GITHUB_API_DEFAULT,
        ),
    )
    return _DEFAULTS


def _reader(source: Any):
    if isinstance(source, Mapping):
        return lambda key, default=None: source.get(key, default)
    return lambda key, default=None: getattr(source, key, default)


def resolve(source: Any = None, *, base: MediaSettings | None = None) -> MediaSettings:
    """把 Mapping / core Settings / MediaSettings 归一为 :class:`MediaSettings`。

    纯函数：不读数据库、不碰磁盘，只看传入对象。未知键忽略（它们不属于媒体层）。
    """

    if isinstance(source, MediaSettings):
        return source
    start = base or defaults()
    if source is None:
        return start
    read = _reader(source)

    # 数据目录：核心 Settings 用 property 暴露 frames_dir/thumbs_dir，也可能只给 data_dir。
    data_dir = _as_path(read("data_dir", start.data_dir), start.data_dir)
    frames_dir = _as_path(read("frames_dir", None), data_dir / "frames")
    thumbs_dir = _as_path(read("thumbs_dir", None), data_dir / "thumbs")

    return MediaSettings(
        data_dir=data_dir,
        frames_dir=frames_dir,
        thumbs_dir=thumbs_dir,
        max_frame_bytes=_as_int(
            read("max_frame_bytes", start.max_frame_bytes), start.max_frame_bytes, minimum=64 * 1024
        ),
        max_width=_as_int(read("max_width", start.max_width), start.max_width, minimum=64, maximum=7680),
        # 旧键 jpeg_quality 与 webp_quality 同义：受控端 JPEG、服务端 WebP，画质只有一个旋钮。
        webp_quality=_as_int(
            read("webp_quality", read("jpeg_quality", start.webp_quality)), start.webp_quality, minimum=30, maximum=95
        ),
        thumb_width=_as_int(read("thumb_width", start.thumb_width), start.thumb_width, minimum=48, maximum=1024),
        transcode_concurrency=_as_int(
            read("transcode_concurrency", start.transcode_concurrency), start.transcode_concurrency, minimum=1, maximum=8
        ),
        frame_retention_days=_as_int(
            read("frame_retention_days", start.frame_retention_days), start.frame_retention_days, minimum=1, maximum=3650
        ),
        audit_retention_days=_as_int(
            read("audit_retention_days", start.audit_retention_days), start.audit_retention_days, minimum=1, maximum=3650
        ),
        command_retention_days=_as_int(
            read("command_retention_days", start.command_retention_days), start.command_retention_days, minimum=1
        ),
        storage_quota_mb=_as_int(
            read("storage_quota_mb", start.storage_quota_mb), start.storage_quota_mb, minimum=0
        ),
        quota_target=_as_int(read("quota_target", start.quota_target), start.quota_target, minimum=1, maximum=100),
        orphan_grace_ms=_as_int(
            read("orphan_grace_ms", start.orphan_grace_ms), start.orphan_grace_ms, minimum=0
        ),
        archive_enabled=_as_bool(read("archive_enabled", start.archive_enabled), start.archive_enabled),
        archive_repo=_as_text(read("archive_repo", start.archive_repo), start.archive_repo),
        archive_token=_as_text(read("archive_token", start.archive_token), start.archive_token),
        archive_branch=_as_text(read("archive_branch", start.archive_branch), start.archive_branch),
        archive_path_prefix=_as_text(read("archive_path_prefix", start.archive_path_prefix), start.archive_path_prefix),
        archive_api_base=_as_text(
            read("archive_api_base", read("github_api_base", start.archive_api_base)), start.archive_api_base
        ),
        archive_verify_readback=_as_bool(
            read("archive_verify_readback", start.archive_verify_readback), start.archive_verify_readback
        ),
        archive_remote_delete=_as_bool(
            read("archive_remote_delete", start.archive_remote_delete), start.archive_remote_delete
        ),
        archive_gate_retention=_as_bool(
            read("archive_gate_retention", start.archive_gate_retention), start.archive_gate_retention
        ),
        archive_gate_grace_days=_as_int(
            read("archive_gate_grace_days", start.archive_gate_grace_days),
            start.archive_gate_grace_days,
            minimum=0,
        ),
    )


# --------------------------------------------------------------------------
# 唯一 IO 入口
# --------------------------------------------------------------------------
def _decode_stored_value(raw: Any) -> Any:
    """settings 表里存的是 JSON；用 text() 读回来时可能是字符串。"""

    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 老库里可能存在未经 JSON 编码的裸字符串；按原值使用，而不是丢弃整条配置。
            return raw
    return raw


async def load(session: AsyncSession, *, base: MediaSettings | None = None) -> MediaSettings:
    """从 ``settings`` 表读运行时配置并归一。

    只做"取值 + 类型归一"，不做范围校验（那是 console/settings 的单一职责）。
    表不存在（骨架期）或单行损坏时退回默认值并记日志，绝不让保留/摄取因配置读取失败而停摆。

    W7（F55）：结果带 30s TTL 缓存——此前每帧上传都全表扫一遍 settings，
    高频采集下是纯浪费；设置页保存经 console/settings_store（独立缓存），
    媒体层最多延迟 30s 收敛，与保留策略的运行节奏匹配。
    """

    now = time.monotonic()
    cached = _LOAD_CACHE.get("value")
    loaded_at = float(_LOAD_CACHE.get("at", 0.0))
    if cached is not None and now - loaded_at < _LOAD_CACHE_TTL_S:
        return cached if base is None else resolve({}, base=base)

    start = base or defaults()
    values: dict[str, Any] = {}
    try:
        rows = (await session.execute(text("SELECT key, value FROM settings"))).all()
    except Exception as exc:  # noqa: BLE001 - 配置读失败必须降级且可观测，不能让存储链停摆
        log.warning("读取 settings 表失败，媒体层使用默认配置: %s", exc)
        return start

    for key, raw in rows:
        values[str(key)] = _decode_stored_value(raw)
    resolved = resolve(values, base=start)
    if base is None:
        _LOAD_CACHE["value"] = resolved
        _LOAD_CACHE["at"] = now
    return resolved


__all__ = [
    "DAY_MS",
    "MediaSettings",
    "configure",
    "defaults",
    "load",
    "reset_cache",
    "resolve",
    "static_config",
]