"""Single source of runtime configuration and product version.

Every environment knob is read here (``OBS_`` prefix) and nowhere else. Keys
that used to exist but were never consumed (``frames_per_bucket``,
``max_upload_bytes``) were deleted instead of being kept as placebo (C8).

Domain defaults live in this module only (A23); any other module that needs a
hostname/URL reads it from :class:`Settings`.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCT = "Observatory"
FALLBACK_VERSION = "0.3.0"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

#: Lowest agent build that may replace itself through the v3 update path (A3/A18).
MIN_SELF_UPDATE_VERSION = "0.2.5"

DEFAULT_CONSOLE_HOST = "control.tianhw.top"
DEFAULT_AGENT_HOSTS = "observatory.123423.xyz,control.tianhw.top"


def _default_data_dir() -> Path:
    env = os.environ.get("OBS_DATA_DIR")
    if env:
        return Path(env)
    if Path("/data").exists():
        return Path("/data")
    return Path(__file__).resolve().parent.parent.parent / ".localdata"


class Settings(BaseSettings):
    """Environment-backed settings; instantiate through :func:`get_settings`."""

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
    enroll_code: str = ""
    session_ttl_minutes: int = Field(default=720, ge=5, le=60 * 24 * 30)

    # ---- storage ------------------------------------------------------------
    data_dir: Path = Field(default_factory=_default_data_dir)
    db_filename: str = "observatory.db"
    max_frame_bytes: int = Field(default=6 * 1024 * 1024, ge=64 * 1024)

    # ---- domains (A11/A23) --------------------------------------------------
    console_host: str = DEFAULT_CONSOLE_HOST
    agent_hosts: str = DEFAULT_AGENT_HOSTS
    public_base_urls: str = f"https://{DEFAULT_CONSOLE_HOST},https://observatory.123423.xyz"
    trust_forwarded_for: bool = False
    allow_loopback_console: bool = True

    # ---- link / presence (spec §4.1) ---------------------------------------
    heartbeat_interval_s: int = Field(default=20, ge=5, le=300)
    poll_hold_s: int = Field(default=20, ge=1, le=20)
    agent_offline_after_s: int = Field(default=120, ge=20, le=3600)
    capture_stale_s: int = Field(default=180, ge=10, le=3600)
    outbox_max_messages: int = Field(default=256, ge=16, le=4096)
    seen_cache_ttl_s: int = Field(default=300, ge=30, le=3600)
    seen_cache_max: int = Field(default=10_000, ge=100, le=1_000_000)

    # ---- timeout budget (spec §5.4, written once) --------------------------
    max_command_deadline_ms: int = Field(default=25_000, ge=1_000, le=120_000)
    command_timeout_files_ms: int = Field(default=25_000, ge=1_000)
    command_timeout_update_ms: int = Field(default=60_000, ge=1_000)
    command_timeout_uninstall_ms: int = Field(default=60_000, ge=1_000)
    command_timeout_sysinfo_ms: int = Field(default=60_000, ge=1_000)
    command_timeout_default_ms: int = Field(default=20_000, ge=1_000)

    # ---- security -----------------------------------------------------------
    login_rate_limit: int = Field(default=8, ge=1)
    login_rate_window_s: int = Field(default=300, ge=10)
    enroll_rate_limit: int = Field(default=20, ge=1)
    enroll_rate_window_s: int = Field(default=300, ge=10)
    link_rate_limit: int = Field(default=240, ge=1)
    link_rate_window_s: int = Field(default=60, ge=1)
    #: Per-IP backstop: rotating tokens cannot escape one address's bucket.
    link_ip_rate_limit: int = Field(default=1200, ge=1)

    # ---- behaviour ----------------------------------------------------------
    log_level: str = "INFO"
    housekeeping_interval_s: int = Field(default=20, ge=5, le=3600)

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(os.path.expandvars(os.path.expanduser(value)))
        return value

    # -- derived: domains -----------------------------------------------------
    @property
    def console_hosts(self) -> list[str]:
        return _split(self.console_host)

    @property
    def agent_host_list(self) -> list[str]:
        hosts = _split(self.agent_hosts)
        for host in self.console_hosts:
            if host not in hosts:
                hosts.append(host)
        return hosts

    @property
    def base_urls(self) -> list[str]:
        urls = [url.rstrip("/") for url in _split(self.public_base_urls) if url.strip()]
        if not urls:
            urls = [f"https://{host}" for host in self.agent_host_list]
        return urls

    # -- derived: thresholds (presence must not hardcode these) --------------
    @property
    def ws_stale_ms(self) -> int:
        return 3 * self.heartbeat_interval_s * 1000

    @property
    def poll_stale_ms(self) -> int:
        return self.poll_hold_s * 1000 + 60_000

    @property
    def offline_after_ms(self) -> int:
        return self.agent_offline_after_s * 1000

    @property
    def capture_stale_ms(self) -> int:
        return self.capture_stale_s * 1000

    def command_timeout_ms(self, kind: str) -> int:
        """Raw per-type cap from §5.4, before the global deadline ceiling."""

        return {
            "files": self.command_timeout_files_ms,
            "update": self.command_timeout_update_ms,
            "uninstall": self.command_timeout_uninstall_ms,
            "sysinfo": self.command_timeout_sysinfo_ms,
        }.get(kind, self.command_timeout_default_ms)

    # -- storage layout -------------------------------------------------------
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
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def webui_dir(self) -> Path:
        return self.data_dir / "webui"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.frames_dir,
            self.thumbs_dir,
            self.packages_dir,
            self.staging_dir,
            self.tmp_dir,
            self.backups_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _split(value: str) -> list[str]:
    seen: list[str] = []
    for item in (value or "").replace(";", ",").split(","):
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


# ---------------------------------------------------------------- version -----
def _candidate_roots() -> list[Path]:
    here = Path(__file__).resolve()
    return [here.parent, here.parent.parent, here.parent.parent.parent, Path.cwd()]


def resolve_version() -> str:
    """Read ``VERSION`` walking up from this module; fall back to the constant."""

    for root in _candidate_roots():
        candidate = root / "VERSION"
        try:
            text = candidate.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
        if VERSION_RE.match(text):
            return text
    return FALLBACK_VERSION


def parse_version(value: str) -> tuple[int, int, int]:
    text = (value or "").strip()
    if not VERSION_RE.match(text):
        raise ValueError(f"invalid version: {value!r}")
    major, minor, patch = text.split(".")
    return int(major), int(minor), int(patch)


def version_tuple(value: str | None = None) -> tuple[int, int, int]:
    return parse_version(value or resolve_version())


def is_newer(candidate: str, than: str | None = None) -> bool:
    try:
        other = parse_version(than) if than else version_tuple()
        return parse_version(candidate) > other
    except ValueError:
        return False


def can_self_update(current_version: str | None) -> bool:
    """True when *current_version* may apply a v3 update without bricking (A3).

    Unknown or malformed versions return ``False``: keeping an old agent running
    is always safer than offering an upgrade it cannot finish.
    """

    try:
        return parse_version(current_version or "0.0.0") >= parse_version(MIN_SELF_UPDATE_VERSION)
    except ValueError:
        return False


__all__ = [
    "DEFAULT_AGENT_HOSTS",
    "DEFAULT_CONSOLE_HOST",
    "FALLBACK_VERSION",
    "MIN_SELF_UPDATE_VERSION",
    "PRODUCT",
    "Settings",
    "VERSION_RE",
    "can_self_update",
    "get_settings",
    "is_newer",
    "parse_version",
    "reset_settings_cache",
    "resolve_version",
    "version_tuple",
]