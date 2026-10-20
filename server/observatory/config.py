"""Runtime configuration.

Everything is read from the environment with the ``OBS_`` prefix so a container
can be configured without editing files. Defaults are the production domain
layout; local development only has to override the data dir and secrets.

Domain policy (requirement: dual-track domains):

* ``console_host`` -- the *only* hostname allowed to serve the console UI.
* ``agent_hosts``  -- every hostname that serves the install script and the
  device-facing API. Both domains are listed so a machine that cannot reach one
  can still enroll and upload through the other.

The agent receives ``public_base_urls`` (an ordered list) at enrollment and
falls over between them on its own.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCT = "Observatory"


def _default_data_dir() -> Path:
    env = os.environ.get("OBS_DATA_DIR")
    if env:
        return Path(env)
    if Path("/data").exists():
        return Path("/data")
    return Path(__file__).resolve().parent.parent.parent / ".localdata"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OBS_",
        env_file=(".env", "server/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- identity / secrets -------------------------------------------------
    secret_key: str = "change-me-in-production"
    admin_password: str = "change-me-in-production"
    enroll_code: str = ""  # empty => enrollment disabled
    session_ttl_minutes: int = Field(default=720, ge=5, le=60 * 24 * 30)

    # ---- storage ------------------------------------------------------------
    data_dir: Path = Field(default_factory=_default_data_dir)
    db_filename: str = "observatory.db"
    frames_per_bucket: int = Field(default=1, ge=1, le=10)

    # ---- domains ------------------------------------------------------------
    console_host: str = "control.tianhw.top"
    agent_hosts: str = "observatory.123423.xyz,control.tianhw.top"
    public_base_urls: str = "https://control.tianhw.top,https://observatory.123423.xyz"

    # ---- limits -------------------------------------------------------------
    max_frame_bytes: int = Field(default=6 * 1024 * 1024, ge=64 * 1024)
    max_upload_bytes: int = Field(default=512 * 1024 * 1024, ge=1024 * 1024)
    login_rate_limit: int = 8
    login_rate_window_s: int = 300
    enroll_rate_limit: int = 20
    enroll_rate_window_s: int = 300

    # ---- server behaviour ---------------------------------------------------
    ws_heartbeat_s: int = 20
    ws_idle_timeout_s: int = 90
    poll_hold_s: int = 25
    agent_offline_after_s: int = 120
    trust_forwarded_for: bool = True

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(os.path.expandvars(os.path.expanduser(value)))
        return value

    # -- derived ------------------------------------------------------------
    @property
    def console_hosts(self) -> list[str]:
        return _split(self.console_host)

    @property
    def agent_host_list(self) -> list[str]:
        hosts = _split(self.agent_hosts)
        # The console host always serves the device API too; merging here means a
        # misconfigured deployment cannot accidentally lock devices out.
        for host in self.console_hosts:
            if host not in hosts:
                hosts.append(host)
        return hosts

    @property
    def base_urls(self) -> list[str]:
        urls = [u.rstrip("/") for u in _split(self.public_base_urls) if u.strip()]
        if not urls:
            urls = [f"https://{h}" for h in self.agent_host_list]
        return urls

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def frames_dir(self) -> Path:
        return self.data_dir / "frames"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def packages_dir(self) -> Path:
        return self.data_dir / "packages"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def webui_dir(self) -> Path:
        return self.data_dir / "webui"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.frames_dir,
            self.thumbs_dir,
            self.packages_dir,
            self.staging_dir,
            self.tmp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _split(value: str) -> list[str]:
    seen: list[str] = []
    for item in value.replace(";", ",").split(","):
        cleaned = item.strip().rstrip("/")
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


def reset_settings_cache() -> None:
    """Testing helper: drop the cached settings object."""

    get_settings.cache_clear()