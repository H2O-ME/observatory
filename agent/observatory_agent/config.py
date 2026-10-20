"""Agent configuration, paths and identifiers.

Everything the agent persists lives under ``%ProgramData%\\Observatory``:

``config.json``  device identity (uid, token), server URLs and the last config
``logs/``        rolling text logs only — never a SQLite database, to keep the
                 agent install footprint boring and auditable
``spool/``       frames that could not be uploaded yet
``staging/``     unpacked update payloads awaiting the swap
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- identity ---
PRODUCT = "Observatory"
SERVICE_NAME = "ObservatoryAgent"
SERVICE_DISPLAY_NAME = "Observatory 受控端"
SERVICE_DESCRIPTION = "Observatory 受控端：按授权采集画面并接受控制台指令。"
PIPE_PREFIX = r"\\.\pipe\observatory-agent"
CONFIG_FILENAME = "config.json"

#: Default domains, used until enrollment returns the authoritative list.
DEFAULT_BASE_URLS = ("https://control.tianhw.top", "https://observatory.123423.xyz")


def program_data_dir() -> Path:
    override = os.environ.get("OBS_PROGRAM_DATA")
    if override:
        return Path(override)
    root = os.environ.get("ProgramData") or os.environ.get("ALLUSERSPROFILE") or r"C:\ProgramData"
    return Path(root) / PRODUCT


PROGRAM_DATA = program_data_dir()
LOG_DIR = PROGRAM_DATA / "logs"
SPOOL_DIR = PROGRAM_DATA / "spool"
STAGING_DIR = PROGRAM_DATA / "staging"
CONFIG_PATH = PROGRAM_DATA / CONFIG_FILENAME

#: Where this build lives. Used by the updater and the uninstaller.
INSTALL_DIR = Path(os.environ.get("OBS_INSTALL_DIR") or Path(__file__).resolve().parent.parent)


def ensure_dirs() -> None:
    for path in (PROGRAM_DATA, LOG_DIR, SPOOL_DIR, STAGING_DIR):
        path.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ config ---
DEFAULT_CAPTURE: dict[str, Any] = {
    "capture_enabled": True,
    "capture_interval_s": 30,
    "capture_on_change_only": False,
    "capture_change_threshold": 8,
    "jpeg_quality": 72,
    "max_width": 1600,
    "thumb_width": 320,
    "capture_scope": "session",
    "ws_enabled": True,
    "poll_interval_s": 5,
    "transport_reprobe_s": 120,
    "control_enabled": True,
    "control_fps": 8,
    "control_quality": 55,
    "file_write_enabled": True,
    "file_upload_max_mb": 512,
    "auto_update": True,
    "heartbeat_interval_s": 20,
}


@dataclass
class AgentConfig:
    device_uid: str = ""
    token: str = ""
    base_urls: list[str] = field(default_factory=lambda: list(DEFAULT_BASE_URLS))
    enroll_code: str = ""
    server_version: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    last_server_time_ms: int = 0
    clock_offset_ms: int = 0

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "AgentConfig":
        target = path or CONFIG_PATH
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        config = cls(
            device_uid=str(raw.get("device_uid") or ""),
            token=str(raw.get("token") or ""),
            base_urls=[str(url) for url in raw.get("base_urls") or [] if str(url).strip()]
            or list(DEFAULT_BASE_URLS),
            enroll_code=str(raw.get("enroll_code") or ""),
            server_version=str(raw.get("server_version") or ""),
            settings=raw.get("settings") if isinstance(raw.get("settings"), dict) else {},
            last_server_time_ms=int(raw.get("last_server_time_ms") or 0),
            clock_offset_ms=int(raw.get("clock_offset_ms") or 0),
        )
        if not config.device_uid:
            config.device_uid = new_device_uid()
        return config

    def save(self, path: Path | None = None) -> None:
        target = path or CONFIG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "device_uid": self.device_uid,
            "token": self.token,
            "base_urls": self.base_urls,
            "enroll_code": self.enroll_code,
            "server_version": self.server_version,
            "settings": self.settings,
            "last_server_time_ms": self.last_server_time_ms,
            "clock_offset_ms": self.clock_offset_ms,
        }
        tmp = target.with_suffix(".json.part")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)

    # -- runtime helpers ---------------------------------------------------
    def setting(self, key: str, default: Any = None) -> Any:
        if key in self.settings:
            return self.settings[key]
        return DEFAULT_CAPTURE.get(key, default)

    def apply_server_config(self, values: dict[str, Any], server_time_ms: int = 0) -> bool:
        """Merge server-provided settings; returns True when something changed."""

        changed = values != self.settings
        self.settings = dict(values)
        if server_time_ms:
            self.last_server_time_ms = int(server_time_ms)
            self.clock_offset_ms = int(server_time_ms) - int(_now_ms())
        return changed


def new_device_uid() -> str:
    """Stable, opaque device identifier.

    Derived from the Windows ``MachineGuid`` so reinstalling the agent on the same
    machine keeps its identity (the token is rotated on re-enroll). Falls back to
    a random identifier when the registry is unavailable.
    """

    machine = _machine_guid()
    if machine:
        return f"OBS-{machine.replace('-', '').upper()[:12]}"
    return f"OBS-{secrets.token_hex(6).upper()}"


def _machine_guid() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except Exception:  # noqa: BLE001 - not fatal, just a different identity
        return ""


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


class ConfigStore:
    """Thread-safe wrapper shared by the service runner and worker children."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or CONFIG_PATH
        self._lock = threading.RLock()
        self._config = AgentConfig.load(self._path)

    @property
    def config(self) -> AgentConfig:
        with self._lock:
            return self._config

    def update(self, **changes: Any) -> AgentConfig:
        with self._lock:
            for key, value in changes.items():
                setattr(self._config, key, value)
            self._config.save(self._path)
            return self._config

    def apply_server_config(self, values: dict[str, Any], server_time_ms: int = 0) -> bool:
        with self._lock:
            changed = self._config.apply_server_config(values, server_time_ms)
            if changed:
                self._config.save(self._path)
            return changed


__all__ = [
    "CONFIG_PATH",
    "DEFAULT_BASE_URLS",
    "DEFAULT_CAPTURE",
    "AgentConfig",
    "ConfigStore",
    "INSTALL_DIR",
    "LOG_DIR",
    "PIPE_PREFIX",
    "PRODUCT",
    "PROGRAM_DATA",
    "SERVICE_DESCRIPTION",
    "SERVICE_DISPLAY_NAME",
    "SERVICE_NAME",
    "SPOOL_DIR",
    "STAGING_DIR",
    "ensure_dirs",
    "new_device_uid",
    "program_data_dir",
]