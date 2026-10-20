"""SQLAlchemy models.

All timestamps are epoch milliseconds stored as BIGINT. There is no naive
datetime anywhere: mixing local time, UTC and server time is exactly what made
the previous console disagree with itself about whether a device was online.
Every API response also carries ``server_time_ms`` so the browser never has to
guess.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_ms() -> int:
    return int(time.time() * 1000)


class Base(DeclarativeBase):
    pass


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

    # Per-device overrides of capture/collection settings. Only keys present here
    # differ from the server-wide defaults.
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Latest reported hardware/OS/agent snapshot, an opaque JSON blob whose shape
    # is owned by the agent's ``sysinfo.collect``.
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
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    session: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="screen")

    path: Mapped[str] = mapped_column(String(512))
    thumb_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")


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
    actor: Mapped[str] = mapped_column(String(64), default="console")
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    dispatched_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    finished_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, default=0)


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
    """A downloadable agent build (the auto-update source of truth)."""

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


__all__ = [
    "AuditEntry",
    "Base",
    "Command",
    "Device",
    "Frame",
    "Package",
    "Setting",
    "now_ms",
]