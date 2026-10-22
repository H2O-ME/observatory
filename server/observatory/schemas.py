"""Pydantic schemas for every public/console/agent payload.

This module is the single contract between the three code bases. Anything the
WebUI renders comes from here, and every payload that describes liveness embeds
``server_time_ms`` so the browser can reason about staleness using the server's
clock instead of its own (which is what produced the "still reporting / already
offline" contradictions before).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PresenceState = Literal["online", "degraded", "offline", "uninstalling", "uninstalled"]
TransportKind = Literal["ws", "poll", "none"]
CommandStatus = Literal["pending", "dispatched", "ok", "error", "timeout", "cancelled"]


class ApiBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


# ------------------------------------------------------------------ presence ---
class CaptureHealthOut(ApiBase):
    """Whether the agent currently has a desktop it can actually capture.

    A service runs in session 0, so a host with nobody logged in has no screen to
    grab. Reporting this explicitly is what stops the console from showing an
    "online" device next to a timeline that never fills.
    """

    state: str = ""
    reason: str = ""
    sessions: int | None = None
    workers: list[int] = Field(default_factory=list)
    at_ms: int = 0


class PresenceOut(ApiBase):
    state: PresenceState
    transport: TransportKind
    reason: str = ""
    last_seen_ms: int = 0
    age_ms: int | None = None
    channel_since_ms: int | None = None
    # Which features actually work right now. The UI must gate on these rather
    # than inventing its own idea of "online".
    capabilities: dict[str, bool] = Field(default_factory=dict)
    capture: CaptureHealthOut | None = None
    # Why the console must not wait for an automatic upgrade, or None when the
    # agent is new enough to upgrade itself. The server never pushes ``update`` to
    # a device that reports a reason here (see services.packages.can_self_update).
    self_update_blocked: str | None = None


class DeviceOut(ApiBase):
    uid: str
    name: str
    hostname: str
    os_name: str
    os_version: str
    arch: str
    agent_version: str
    enrolled_at_ms: int
    last_seen_ms: int
    note: str = ""
    presence: PresenceOut
    sysinfo: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    latest_frame_id: int | None = None
    server_time_ms: int = 0


class DeviceListOut(ApiBase):
    devices: list[DeviceOut]
    server_time_ms: int
    server_version: str


class DevicePatch(ApiBase):
    name: str | None = None
    note: str | None = None
    settings: dict[str, Any] | None = None


class DeviceEnvelope(ApiBase):
    device: DeviceOut
    server_time_ms: int


# ------------------------------------------------------------------ commands ---
class CommandOut(ApiBase):
    id: str
    type: str
    status: CommandStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at_ms: int
    dispatched_at_ms: int | None = None
    finished_at_ms: int | None = None


class CommandCreate(ApiBase):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False


class CommandListOut(ApiBase):
    commands: list[CommandOut]
    server_time_ms: int


class UninstallRequest(ApiBase):
    """The console MUST send this body.

    The old endpoint declared a required body while the frontend posted none,
    so remote uninstall was a guaranteed 422. Keeping the model required and the
    client explicit makes the mismatch impossible to reintroduce silently.
    """

    confirm: bool = Field(..., description="必须显式为 true")


class UninstallAccepted(ApiBase):
    command_id: str
    status: CommandStatus
    message: str


# -------------------------------------------------------------------- frames ---
class FrameOut(ApiBase):
    id: int
    ts_ms: int
    width: int
    height: int
    size_bytes: int
    has_thumb: bool
    kind: str = "screen"


class LatestFrameOut(ApiBase):
    frame: FrameOut | None = None
    server_time_ms: int


class TimelineBucketOut(ApiBase):
    ts_ms: int
    frame_id: int | None
    frame_count: int
    session: int = 0


class TimelineOut(ApiBase):
    from_ms: int
    to_ms: int
    bucket_ms: int
    buckets: list[TimelineBucketOut]
    total_frames: int
    server_time_ms: int


class FrameListOut(ApiBase):
    frames: list[FrameOut]
    server_time_ms: int


# --------------------------------------------------------------------- files ---
class FileEntryOut(ApiBase):
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime_ms: int = 0
    hidden: bool = False
    drive: str = ""


class FileListOut(ApiBase):
    path: str
    parent: str | None = None
    entries: list[FileEntryOut] = Field(default_factory=list)
    drives: list[FileEntryOut] = Field(default_factory=list)
    truncated: bool = False
    server_time_ms: int


class FileOpRequest(ApiBase):
    op: Literal[
        "list",
        "stat",
        "mkdir",
        "delete",
        "rename",
        "copy",
        "move",
        "search",
        "drives",
        "read",
        "write",
        "hash",
    ]
    path: str = ""
    target: str | None = None
    recursive: bool = False
    pattern: str | None = None
    handle: str | None = None
    offset: int = 0
    length: int = 0
    data: str | None = Field(default=None, description="base64 分片")
    truncate: bool = False


class FileOpResult(ApiBase):
    ok: bool
    op: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int = 0


class FileReadOut(ApiBase):
    handle: str
    offset: int
    length: int
    size: int
    eof: bool
    data: str


# -------------------------------------------------------------------- control ---
class ControlGrantIn(ApiBase):
    confirm: bool = True
    ttl_s: int = Field(default=300, ge=30, le=3600)


class ControlGrantOut(ApiBase):
    token: str
    expires_at_ms: int
    fps: int
    quality: int
    ws_path: str
    server_time_ms: int


class ControlInputIn(ApiBase):
    """One injected input event.

    ``kind`` selects which fields matter: ``mouse`` uses x/y/button/action,
    ``wheel`` uses delta, ``key`` uses code/key/text/modifiers.
    """

    kind: Literal["mouse", "wheel", "key", "text"]
    action: Literal["move", "down", "up", "click"] = "move"
    x: int = 0
    y: int = 0
    button: int = 0
    delta: int = 0
    code: int = 0
    key: str = ""
    text: str = ""
    modifiers: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- auth ----
class LoginIn(ApiBase):
    password: str
    totp: str | None = None


class LoginOut(ApiBase):
    token: str
    expires_at_ms: int
    version: str


class SessionOut(ApiBase):
    authenticated: bool
    totp_enabled: bool
    version: str
    server_time_ms: int
    console_host: str
    base_urls: list[str] = Field(default_factory=list)


class TotpSetupOut(ApiBase):
    secret: str
    uri: str


class TotpEnableIn(ApiBase):
    secret: str
    code: str


class TotpDisableIn(ApiBase):
    code: str = Field(..., min_length=6, description="关闭二次验证必须校验验证码")


class PasswordChangeIn(ApiBase):
    current_password: str
    new_password: str = Field(..., min_length=10)


class OkOut(ApiBase):
    ok: bool = True
    message: str = ""


# ------------------------------------------------------------------ settings ---
class SettingsOut(ApiBase):
    values: dict[str, Any]
    specs: dict[str, dict[str, Any]]
    server_version: str
    server_time_ms: int


class SettingsPatch(ApiBase):
    values: dict[str, Any]


# ------------------------------------------------------------------- updates ---
class PackageOut(ApiBase):
    version: str
    filename: str
    size_bytes: int
    sha256: str
    notes: str
    created_at_ms: int
    download_url: str


class PackageListOut(ApiBase):
    packages: list[PackageOut]
    latest_version: str | None
    server_version: str
    server_time_ms: int


class UpdateInfoOut(ApiBase):
    version: str
    url: str
    sha256: str
    size_bytes: int
    mandatory: bool = False


class UpdateStatusOut(ApiBase):
    server_version: str
    latest_agent_version: str | None
    outdated: list[dict[str, str]]
    packages: list[PackageOut]
    server_time_ms: int


# --------------------------------------------------------------------- agent ---
class SessionInfoIn(ApiBase):
    session_id: int
    username: str = ""
    state: str = ""
    is_console: bool = False


class EnrollIn(ApiBase):
    code: str
    uid: str
    hostname: str = ""
    os_name: str = ""
    os_version: str = ""
    arch: str = ""
    agent_version: str = ""
    sysinfo: dict[str, Any] = Field(default_factory=dict)
    sessions: list[SessionInfoIn] = Field(default_factory=list)


class EnrollOut(ApiBase):
    device_uid: str
    token: str
    config: dict[str, Any]
    base_urls: list[str]
    server_version: str
    server_time_ms: int
    heartbeat_interval_s: int


class HeartbeatIn(ApiBase):
    agent_version: str = ""
    sysinfo: dict[str, Any] | None = None
    state: dict[str, Any] = Field(default_factory=dict)


class HeartbeatOut(ApiBase):
    config: dict[str, Any]
    server_time_ms: int
    heartbeat_interval_s: int
    poll_hold_s: int
    update: UpdateInfoOut | None = None
    uninstall: bool = False
    commands: list[CommandOut] = Field(default_factory=list)
    acknowledged_uninstall: bool = False


class CommandResultIn(ApiBase):
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None


class FrameUploadMeta(ApiBase):
    ts_ms: int
    width: int = 0
    height: int = 0
    session: int = 0
    kind: str = "screen"
    sha256: str = ""


class FrameUploadOut(ApiBase):
    frame_id: int
    ts_ms: int
    stored: bool = True
    server_time_ms: int


class AgentHello(ApiBase):
    uid: str
    agent_version: str
    transport: str = "ws"
    sessions: list[SessionInfoIn] = Field(default_factory=list)
    capabilities: dict[str, bool] = Field(default_factory=dict)


class VersionOut(ApiBase):
    version: str
    product: str = "Observatory"
    server_time_ms: int


# ---------------------------------------------------------------------- wall ---
class EventOut(ApiBase):
    """Server->console event envelope (used on the admin WebSocket)."""

    type: str
    ts_ms: int
    device_uid: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class AuditOut(ApiBase):
    id: int
    ts_ms: int
    actor: str
    action: str
    device_uid: str
    device_name: str
    ip: str
    detail: dict[str, Any]


class AuditListOut(ApiBase):
    entries: list[AuditOut]
    total: int
    server_time_ms: int


__all__ = [name for name in dir() if name.endswith(("In", "Out", "Request", "Patch", "Create", "Accepted"))]