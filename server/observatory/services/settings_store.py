"""Server-wide settings with a single validator table.

The previous console let the API write values the agent could not honour (e.g.
``capture_change_threshold`` accepted 0..32 while the agent expected 1..64),
which is one reason the UI and reality disagreed. Every knob below therefore
declares its type and range in exactly one place, and both the HTTP layer and the
per-device override path validate through ``normalize``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Setting, now_ms

log = logging.getLogger(__name__)

Kind = Literal["bool", "int", "str", "choice", "optional_str"]


@dataclass(frozen=True)
class Spec:
    kind: Kind
    default: Any
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    per_device: bool = False
    secret: bool = False
    description: str = ""


SPECS: dict[str, Spec] = {
    # ---- capture -----------------------------------------------------------
    "capture_enabled": Spec("bool", True, per_device=True, description="采集总开关"),
    "capture_interval_s": Spec("int", 30, 5, 3600, per_device=True, description="采集间隔（秒）"),
    "capture_on_change_only": Spec("bool", False, per_device=True, description="仅画面变化时保存"),
    "capture_change_threshold": Spec("int", 8, 1, 64, per_device=True, description="画面变化阈值"),
    "jpeg_quality": Spec("int", 72, 30, 95, per_device=True, description="图像质量"),
    "max_width": Spec("int", 1600, 320, 3840, per_device=True, description="最长边像素"),
    "thumb_width": Spec("int", 320, 96, 640, per_device=True, description="缩略图宽度"),
    "capture_scope": Spec("choice", "session", choices=("session", "console"), per_device=True, description="采集目标会话"),
    # ---- transport ---------------------------------------------------------
    "ws_enabled": Spec("bool", True, per_device=True, description="允许 WebSocket 通道"),
    "poll_interval_s": Spec("int", 5, 2, 120, per_device=True, description="轮询间隔（秒）"),
    "transport_reprobe_s": Spec("int", 120, 30, 3600, per_device=True, description="向上探测间隔（秒）"),
    # ---- live control ------------------------------------------------------
    "control_enabled": Spec("bool", True, per_device=True, description="允许实时控制"),
    "control_fps": Spec("int", 8, 1, 15, per_device=True, description="实时画面帧率"),
    "control_quality": Spec("int", 55, 20, 85, per_device=True, description="实时画面质量"),
    # ---- file manager ------------------------------------------------------
    "file_write_enabled": Spec("bool", True, per_device=True, description="允许写入文件"),
    "file_upload_max_mb": Spec("int", 512, 1, 8192, per_device=True, description="单文件上传上限（MB）"),
    # ---- updates -----------------------------------------------------------
    "auto_update": Spec("bool", True, per_device=True, description="允许自动更新"),
    "update_channel": Spec("choice", "stable", choices=("stable", "beta"), description="更新通道"),
    # ---- retention ---------------------------------------------------------
    "frame_retention_days": Spec("int", 14, 1, 365, description="画面保留天数"),
    "audit_retention_days": Spec("int", 180, 7, 3650, description="审计保留天数"),
    "command_retention_days": Spec("int", 30, 1, 365, description="命令保留天数"),
    "storage_quota_mb": Spec("int", 4096, 256, 1_048_576, description="数据卷配额（MB）"),
    "quota_target": Spec("int", 95, 50, 99, description="配额回收目标（百分比）"),
    # ---- archive -----------------------------------------------------------
    "archive_enabled": Spec("bool", False, description="归档到远端仓库"),
    "archive_repo": Spec("str", "", description="归档仓库 owner/name"),
    "archive_token": Spec("str", "", secret=True, description="归档仓库令牌"),
    "archive_branch": Spec("str", "main", description="归档分支"),
    "archive_path_prefix": Spec("str", "frames", description="归档路径前缀"),
    "archive_cache_max_mb": Spec("int", 512, 64, 65536, description="归档缓存上限（MB）"),
    # ---- misc --------------------------------------------------------------
    "heartbeat_interval_s": Spec("int", 20, 5, 300, per_device=True, description="心跳间隔（秒）"),
    "install_script_note": Spec("str", "", description="安装页备注"),
}

DEFAULT_SETTINGS: dict[str, Any] = {key: spec.default for key, spec in SPECS.items()}
WRITABLE: frozenset[str] = frozenset(SPECS)
PER_DEVICE_KEYS: frozenset[str] = frozenset(key for key, spec in SPECS.items() if spec.per_device)
SECRET_KEYS: frozenset[str] = frozenset(key for key, spec in SPECS.items() if spec.secret)


class SettingsError(ValueError):
    """Raised when a caller submits a value outside the declared spec."""


def normalize(key: str, value: Any) -> Any:
    """Coerce and range-check one setting value."""

    spec = SPECS.get(key)
    if spec is None:
        raise SettingsError(f"未知设置项: {key}")

    if value is None:
        raise SettingsError(f"{key} 不能为空")

    if spec.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        raise SettingsError(f"{key} 需要布尔值")

    if spec.kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{key} 需要整数") from exc
        if spec.minimum is not None and number < spec.minimum:
            raise SettingsError(f"{key} 不能小于 {spec.minimum}")
        if spec.maximum is not None and number > spec.maximum:
            raise SettingsError(f"{key} 不能大于 {spec.maximum}")
        return number

    if spec.kind == "choice":
        text = str(value).strip()
        if text not in spec.choices:
            raise SettingsError(f"{key} 只能是 {'/'.join(spec.choices)}")
        return text

    if spec.kind == "optional_str":
        text = str(value).strip()
        return text

    return str(value).strip()


class SettingsStore:
    """Cached view over the ``settings`` table."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    async def load(self, session: AsyncSession) -> dict[str, Any]:
        if self._cache is not None:
            return dict(self._cache)
        rows = (await session.execute(select(Setting))).scalars().all()
        values = dict(DEFAULT_SETTINGS)
        for row in rows:
            if row.key not in SPECS:
                continue
            try:
                values[row.key] = normalize(row.key, row.value)
            except SettingsError:
                log.warning("ignoring invalid stored setting %s=%r", row.key, row.value)
        self._cache = values
        return dict(values)

    async def patch(self, session: AsyncSession, updates: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, raw in updates.items():
            if key not in WRITABLE:
                raise SettingsError(f"设置项不可写: {key}")
            normalized[key] = normalize(key, raw)

        for key, value in normalized.items():
            row = await session.get(Setting, key)
            if row is None:
                session.add(Setting(key=key, value=value, updated_at_ms=now_ms()))
            else:
                row.value = value
                row.updated_at_ms = now_ms()
        await session.commit()
        self.invalidate()
        return normalized

    async def effective(self, session: AsyncSession, device_settings: dict[str, Any] | None) -> dict[str, Any]:
        """Server defaults + stored values + per-device overrides."""

        values = await self.load(session)
        for key, raw in (device_settings or {}).items():
            if key not in PER_DEVICE_KEYS:
                continue
            try:
                values[key] = normalize(key, raw)
            except SettingsError:
                log.warning("ignoring invalid device override %s=%r", key, raw)
        return values

    def invalidate(self) -> None:
        self._cache = None


def public_view(values: dict[str, Any]) -> dict[str, Any]:
    """Drop secrets before sending settings to a browser."""

    return {key: value for key, value in values.items() if key not in SECRET_KEYS}


def describe() -> dict[str, dict[str, Any]]:
    return {
        key: {
            "kind": spec.kind,
            "default": None if spec.secret else spec.default,
            "min": spec.minimum,
            "max": spec.maximum,
            "choices": list(spec.choices),
            "per_device": spec.per_device,
            "secret": spec.secret,
            "description": spec.description,
        }
        for key, spec in SPECS.items()
    }


__all__ = [
    "DEFAULT_SETTINGS",
    "PER_DEVICE_KEYS",
    "SECRET_KEYS",
    "SPECS",
    "WRITABLE",
    "SettingsError",
    "SettingsStore",
    "describe",
    "normalize",
    "public_view",
]