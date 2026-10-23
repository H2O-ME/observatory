"""Runtime (database-backed) setting specs — the one validator table.

This is the new home of the settings contract. ``console.settings`` persists these
rows; the agent-facing projection must go through :func:`public_view` **and** an
agent-safe whitelist, never through a raw load (A4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

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
    agent_visible: bool = True
    description: str = ""


#: Every writable runtime setting. A key that is not here cannot be stored, and
#: a key that is here must have a consumer (C8; asserted by tests in W3).
SPECS: dict[str, Spec] = {
    # ---- capture -----------------------------------------------------------
    "capture_enabled": Spec("bool", True, per_device=True, description="采集总开关"),
    "capture_interval_s": Spec("int", 30, 5, 3600, per_device=True, description="采集间隔（秒）"),
    "capture_on_change_only": Spec("bool", False, per_device=True, description="仅画面变化时保存"),
    "capture_change_threshold": Spec("int", 8, 1, 64, per_device=True, description="画面变化阈值"),
    "jpeg_quality": Spec("int", 72, 30, 95, per_device=True, description="中间图质量"),
    "max_width": Spec("int", 1600, 320, 3840, per_device=True, description="最长边像素"),
    "thumb_width": Spec("int", 320, 96, 640, per_device=True, description="缩略图宽度"),
    "capture_scope": Spec("choice", "session", choices=("session", "console"), per_device=True, description="采集目标会话"),
    # ---- transport ---------------------------------------------------------
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
    # ---- retention ---------------------------------------------------------
    "frame_retention_days": Spec("int", 14, 1, 365, description="画面保留天数"),
    "media_retention_interval_s": Spec("int", 300, 30, 86_400, description="媒体保留执行间隔（秒）"),
    "audit_retention_days": Spec("int", 180, 7, 3650, description="审计保留天数"),
    "command_retention_days": Spec("int", 30, 1, 365, description="命令保留天数"),
    "storage_quota_mb": Spec("int", 4096, 256, 1_048_576, description="数据卷配额（MB）"),
    "quota_target": Spec("int", 95, 50, 99, description="配额回收目标（百分比）"),
    # ---- archive -----------------------------------------------------------
    "archive_enabled": Spec("bool", False, description="归档到远端仓库"),
    "archive_repo": Spec("str", "", description="归档仓库 owner/name"),
    "archive_token": Spec("str", "", secret=True, agent_visible=False, description="归档仓库令牌"),
    "archive_branch": Spec("str", "main", description="归档分支"),
    "archive_path_prefix": Spec("str", "frames", description="归档路径前缀"),
    # ---- misc --------------------------------------------------------------
    "heartbeat_interval_s": Spec("int", 20, 5, 300, per_device=True, description="心跳间隔（秒）"),
    "install_token_ttl_minutes": Spec(
        "int", 1440, 5, 43200, agent_visible=False, description="安装令牌默认有效期（分钟）"
    ),
    "install_script_note": Spec("str", "", description="安装页备注"),
}

DEFAULT_SETTINGS: dict[str, Any] = {key: spec.default for key, spec in SPECS.items()}
WRITABLE: frozenset[str] = frozenset(SPECS)
PER_DEVICE_KEYS: frozenset[str] = frozenset(key for key, spec in SPECS.items() if spec.per_device)
SECRET_KEYS: frozenset[str] = frozenset(key for key, spec in SPECS.items() if spec.secret)
#: The only keys that may ever be sent to an agent (A4: no secrets, ever).
AGENT_VISIBLE_KEYS: frozenset[str] = frozenset(
    key for key, spec in SPECS.items() if spec.agent_visible and not spec.secret
)


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
        return str(value).strip()

    return str(value).strip()


def public_view(values: dict[str, Any]) -> dict[str, Any]:
    """Drop secrets before any value reaches a browser."""

    return {key: value for key, value in values.items() if key not in SECRET_KEYS}


def agent_view(values: dict[str, Any]) -> dict[str, Any]:
    """Project stored settings onto the agent-safe whitelist (A4)."""

    return {key: value for key, value in values.items() if key in AGENT_VISIBLE_KEYS}


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
    "AGENT_VISIBLE_KEYS",
    "DEFAULT_SETTINGS",
    "PER_DEVICE_KEYS",
    "SECRET_KEYS",
    "SPECS",
    "WRITABLE",
    "SettingsError",
    "Spec",
    "agent_view",
    "describe",
    "normalize",
    "public_view",
]