"""SQLAlchemy models — the single Python description of the persisted schema.

The authoritative column list lives in ``docs/0.3.0-重构规格.md`` §6bis. The
migration functions in :mod:`observatory.core.db` create the same shapes in raw
SQL; these models are what the rest of the server queries through the ORM.

Time semantics (A12): every ``*_ms`` column except ``device_ts_ms`` is a server
clock value. ``frames.device_ts_ms`` is the capture timestamp reported by the
device and is therefore the only field that may lag or lead the server clock.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .clock import now_ms


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model and the migration tests."""


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    hostname: Mapped[str] = mapped_column(String(255), default="")
    os_name: Mapped[str] = mapped_column(String(64), default="")
    os_version: Mapped[str] = mapped_column(String(128), default="")
    arch: Mapped[str] = mapped_column(String(32), default="")
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    token_hash: Mapped[str] = mapped_column(String(128), default="")

    enrolled_at_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    last_seen_ms: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    uninstalled_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_result_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    #: Changes across an agent reboot; A2 uses it to prove a restart happened.
    boot_id: Mapped[str] = mapped_column(String(64), default="")
    #: 2 = 0.2.x legacy protocol, 3 = v3 link protocol.
    protocol: Mapped[int] = mapped_column(Integer, default=2)

    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sysinfo: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(255), default="")

    @property
    def display_name(self) -> str:
        return self.name or self.hostname or self.uid


class Frame(Base):
    __tablename__ = "frames"
    __table_args__ = (
        Index("ix_frames_device_ts", "device_id", "ts_ms"),
        Index("ix_frames_device_session", "device_id", "session"),
        UniqueConstraint("device_id", "frame_id", name="ux_frames_device_frame"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)

    #: Server receive time (single clock source).
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    #: Capture time reported by the device; NULL for rows migrated from 0.2.x.
    device_ts_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    session: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="screen")

    #: Idempotency key supplied by the device (X-Frame-Id); NULL on legacy rows.
    frame_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    path: Mapped[str] = mapped_column(String(512))
    thumb_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")

    #: Set once the remote archive confirmed the upload (A14/A21).
    archived_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Command(Base):
    __tablename__ = "commands"
    __table_args__ = (Index("ix_commands_device_status", "device_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    actor: Mapped[str] = mapped_column(String(64), default="console")
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    dispatched_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    finished_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    #: Budget handed to the agent and used to derive the client timeout (§5.4).
    deadline_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AuditEntry(Base):
    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True, default=now_ms)
    actor: Mapped[str] = mapped_column(String(64), default="console")
    action: Mapped[str] = mapped_column(String(64), index=True)
    device_uid: Mapped[str] = mapped_column(String(64), default="", index=True)
    device_name: Mapped[str] = mapped_column(String(128), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class Package(Base):
    __tablename__ = "packages"
    __table_args__ = (UniqueConstraint("kind", "version", name="uq_packages_kind_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), default="agent")
    version: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(String(255), default="")
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class FrameArchive(Base):
    """Per-frame remote archive state machine (A14)."""

    __tablename__ = "frame_archives"
    __table_args__ = (
        Index("ix_frame_archives_device_frame", "device_id", "frame_id"),
        Index("ix_frame_archives_status_created", "status", "created_at_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(Integer, index=True)
    frame_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    day: Mapped[str] = mapped_column(String(16), default="")
    remote_path: Mapped[str] = mapped_column(String(512), default="")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    #: 待上传 | 已上传 | 失败 | 已删除（§6bis 中文取值，与 media.archive 常量一致）
    status: Mapped[str] = mapped_column(String(16), default="待上传")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    uploaded_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class InstallToken(Base):
    """One-shot installer credential (A18); business logic lives in console/install."""

    __tablename__ = "install_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="console")
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    used_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)

    @property
    def remaining_uses(self) -> int:
        return max(0, int(self.max_uses or 1) - (1 if self.used_at_ms else 0))


class DeletedDevice(Base):
    """Delete tombstone (0.3.1 F9, resurrection semantics amended same release).

    A deleted device's token hash lives on here so the link directory can answer
    ``410 GONE`` instead of ``401`` — the agent treats GONE as terminal and stops
    retrying. Enrolment refuses to resurrect the uid **with old credentials**;
    a freshly minted one-time install token pierces the tombstone (the admin
    minting it is the re-admission consent) and burns the row inside the claim
    transaction. Rows are tiny and carry no expiry by design.
    """

    __tablename__ = "deleted_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    deleted_at_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)


__all__ = [
    "AuditEntry",
    "Base",
    "Command",
    "DeletedDevice",
    "Device",
    "Frame",
    "FrameArchive",
    "InstallToken",
    "Package",
    "Setting",
]