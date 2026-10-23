"""Protocol v3 envelope — the only wire definition on the server.

Shape (spec §5.1)::

    {"v": 3, "id": "<uuid4>", "t": "<type>", "ts_ms": <sender clock>, "body": {...}}

Time contract (C5): downlink ``ts_ms`` is **always** the server clock. Uplink
``ts_ms`` is whatever the device claims; server code must not use it for
liveness or ordering (those use the receipt time). Device capture timestamps
travel as ``device_ts_ms`` inside frame upload metadata, never here.

The binary control frame (WS carrier only) is::

    uint32 BE header length | JSON header ({"t": "ctrl", "session": "..."}) | payload
"""

from __future__ import annotations

import json
import logging
import struct
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core.clock import Clock, SYSTEM_CLOCK
from ..core.errors import AppError, ErrorCode

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 3
MAX_HEADER_BYTES = 64 * 1024
_HEADER_STRUCT = struct.Struct(">I")

#: Envelope types the server may send to an agent.
DOWNLINK_TYPES: frozenset[str] = frozenset(
    {"welcome", "config", "command", "control", "ping", "pong", "uninstall", "update_offer"}
)
#: Envelope types an agent may send to the server.
UPLINK_TYPES: frozenset[str] = frozenset(
    {"hello", "heartbeat", "result", "event", "control_frame", "uninstall_ack", "pong"}
)

TERMINAL_RESULT_STATES: frozenset[str] = frozenset({"ok", "error", "timeout", "cancelled"})


class EnvelopeError(AppError):
    """Malformed or unsupported envelope; maps to BAD_REQUEST/UNSUPPORTED."""

    def __init__(self, message: str, *, code: ErrorCode = ErrorCode.BAD_REQUEST) -> None:
        super().__init__(code, message, retryable=False)


class FramingError(EnvelopeError):
    """Malformed binary control frame."""


@dataclass(frozen=True, slots=True)
class Envelope:
    v: int
    id: str
    t: str
    ts_ms: int
    body: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"v": self.v, "id": self.id, "t": self.t, "ts_ms": self.ts_ms, "body": self.body}

    @classmethod
    def from_dict(cls, raw: Any) -> Envelope:
        if not isinstance(raw, dict):
            raise EnvelopeError("信封必须是 JSON 对象")
        version = raw.get("v", PROTOCOL_VERSION)
        if version != PROTOCOL_VERSION:
            raise EnvelopeError(
                f"不支持的协议版本: {version!r}（本服务端只接受 v{PROTOCOL_VERSION}）",
                code=ErrorCode.UNSUPPORTED,
            )
        msg_id = raw.get("id")
        if not isinstance(msg_id, str) or not msg_id:
            raise EnvelopeError("信封缺少 id")
        kind = raw.get("t")
        if not isinstance(kind, str) or not kind:
            raise EnvelopeError("信封缺少 t")
        ts_ms = raw.get("ts_ms", 0)
        if not isinstance(ts_ms, int):
            raise EnvelopeError("信封 ts_ms 必须是整数毫秒")
        body = raw.get("body", {})
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise EnvelopeError("信封 body 必须是对象")
        return cls(v=PROTOCOL_VERSION, id=msg_id, t=kind, ts_ms=ts_ms, body=dict(body))


@dataclass(frozen=True, slots=True)
class UplinkBatch:
    """One HTTP carrier request (spec §5.6: the field is named ``items``)."""

    v: int
    items: tuple[Envelope, ...]
    last_ack_id: str | None = None

    @property
    def protocol(self) -> int:
        return self.v


@dataclass(frozen=True, slots=True)
class BinaryControlFrame:
    session: str
    header: dict[str, Any]
    payload: bytes


@dataclass(frozen=True, slots=True)
class Result:
    """The single result type returned by ``AgentSession.request``."""

    ok: bool
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    command_id: str = ""
    deadline_ms: int = 0
    server_time_ms: int = 0
    latency_ms: int = 0

    @classmethod
    def succeeded(
        cls,
        *,
        command_id: str,
        result: dict[str, Any] | None,
        deadline_ms: int,
        server_time_ms: int,
        latency_ms: int,
    ) -> Result:
        return cls(
            ok=True,
            status="ok",
            result=result,
            command_id=command_id,
            deadline_ms=deadline_ms,
            server_time_ms=server_time_ms,
            latency_ms=latency_ms,
        )

    @classmethod
    def failed(
        cls,
        *,
        command_id: str,
        error: str,
        error_code: str = ErrorCode.INTERNAL.value,
        retryable: bool = False,
        result: dict[str, Any] | None = None,
        deadline_ms: int = 0,
        server_time_ms: int = 0,
        latency_ms: int = 0,
    ) -> Result:
        return cls(
            ok=False,
            status="error",
            result=result,
            error=error,
            error_code=error_code,
            retryable=retryable,
            command_id=command_id,
            deadline_ms=deadline_ms,
            server_time_ms=server_time_ms,
            latency_ms=latency_ms,
        )

    @classmethod
    def timed_out(
        cls,
        *,
        command_id: str,
        error: str,
        deadline_ms: int,
        server_time_ms: int,
        latency_ms: int,
    ) -> Result:
        return cls(
            ok=False,
            status="timeout",
            error=error,
            error_code=ErrorCode.TIMEOUT.value,
            retryable=True,
            command_id=command_id,
            deadline_ms=deadline_ms,
            server_time_ms=server_time_ms,
            latency_ms=latency_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "command_id": self.command_id,
            "deadline_ms": self.deadline_ms,
            "server_time_ms": self.server_time_ms,
            "latency_ms": self.latency_ms,
        }


def new_envelope(
    kind: str,
    body: dict[str, Any] | None = None,
    *,
    msg_id: str | None = None,
    ts_ms: int | None = None,
    clock: Clock | None = None,
) -> Envelope:
    """Build a server-sent envelope with a fresh id and the server clock."""

    now = ts_ms if ts_ms is not None else (clock or SYSTEM_CLOCK).now_ms()
    return Envelope(
        v=PROTOCOL_VERSION,
        id=msg_id or uuid.uuid4().hex,
        t=kind,
        ts_ms=int(now),
        body=dict(body or {}),
    )


def encode_envelope(envelope: Envelope) -> dict[str, Any]:
    return envelope.to_dict()


def decode_envelope(raw: Any) -> Envelope:
    return Envelope.from_dict(raw)


def decode_uplink_batch(payload: Any) -> UplinkBatch:
    """Decode the authoritative HTTP carrier request (spec §5.6).

    Batch: ``{"v": 3, "last_ack_id": "...", "items": [Envelope, ...]}``.
    """

    if not isinstance(payload, dict):
        raise EnvelopeError("上行批次必须是 JSON 对象")
    version = payload.get("v", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise EnvelopeError(
            f"不支持的协议版本: {version!r}（本服务端只接受 v{PROTOCOL_VERSION}）",
            code=ErrorCode.UNSUPPORTED,
        )
    if "items" in payload:
        raw_items = payload.get("items")
    elif "messages" in payload:
        # Transitional tolerance for the W0 draft name; the wire contract is
        # `items` (spec §5.6) and the warning makes the drift visible.
        log.warning("uplink batch used deprecated `messages` field; expected `items`")
        raw_items = payload.get("messages")
    elif "t" in payload:
        raw_items = [payload]
    else:
        raise EnvelopeError("上行批次缺少 items")
    if not isinstance(raw_items, list):
        raise EnvelopeError("items 必须是数组")
    last_ack_id = payload.get("last_ack_id")
    if last_ack_id is not None and not isinstance(last_ack_id, str):
        raise EnvelopeError("last_ack_id 必须是字符串或 null")
    items = tuple(Envelope.from_dict(item) for item in raw_items)
    return UplinkBatch(v=PROTOCOL_VERSION, items=items, last_ack_id=last_ack_id)


def encode_downlink_batch(
    messages: list[Envelope] | tuple[Envelope, ...],
    *,
    ack_id: str = "",
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Build the authoritative response body (spec §5.6).

    ``ack_id`` is the last downlink id handed to the client (or the caller's own
    anchor when the batch is empty), so the client can advance its resume anchor.
    """

    return {
        "v": PROTOCOL_VERSION,
        "ack_id": ack_id or "",
        "server_time_ms": (clock or SYSTEM_CLOCK).now_ms(),
        "items": [message.to_dict() for message in messages],
    }


def encode_binary(header: dict[str, Any], payload: bytes = b"") -> bytes:
    raw = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not raw or len(raw) > MAX_HEADER_BYTES:
        raise FramingError("控制帧头长度非法")
    return _HEADER_STRUCT.pack(len(raw)) + raw + payload


def decode_binary(blob: bytes) -> tuple[dict[str, Any], bytes]:
    if len(blob) < _HEADER_STRUCT.size:
        raise FramingError("控制帧头被截断")
    (length,) = _HEADER_STRUCT.unpack_from(blob, 0)
    if length <= 0 or length > MAX_HEADER_BYTES:
        raise FramingError(f"控制帧头长度非法: {length}")
    start = _HEADER_STRUCT.size
    end = start + length
    if len(blob) < end:
        raise FramingError("控制帧头被截断")
    try:
        header = json.loads(blob[start:end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FramingError("控制帧头不是合法 JSON") from exc
    if not isinstance(header, dict):
        raise FramingError("控制帧头必须是对象")
    return header, blob[end:]


def decode_control_frame(blob: bytes) -> BinaryControlFrame:
    header, payload = decode_binary(blob)
    if header.get("t") != "ctrl":
        raise FramingError(f"不是控制帧: {header.get('t')!r}")
    session = header.get("session")
    if not isinstance(session, str) or not session:
        raise FramingError("控制帧缺少 session")
    return BinaryControlFrame(session=session, header=header, payload=payload)


def encode_control_frame(session: str, payload: bytes = b"") -> bytes:
    return encode_binary({"t": "ctrl", "session": session}, payload)


__all__ = [
    "DOWNLINK_TYPES",
    "MAX_HEADER_BYTES",
    "PROTOCOL_VERSION",
    "TERMINAL_RESULT_STATES",
    "UPLINK_TYPES",
    "BinaryControlFrame",
    "Envelope",
    "EnvelopeError",
    "FramingError",
    "Result",
    "UplinkBatch",
    "decode_binary",
    "decode_control_frame",
    "decode_envelope",
    "decode_uplink_batch",
    "encode_binary",
    "encode_control_frame",
    "encode_downlink_batch",
    "encode_envelope",
    "new_envelope",
]