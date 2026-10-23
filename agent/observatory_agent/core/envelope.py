"""协议 v3 信封：编码、解码、校验、批次与二进制帧格式（唯一协议定义）。

与服务端 ``server/observatory/link/envelope.py`` 是**同构的独立副本**：受控端
被 PyInstaller 打成一个 onedir 包，不能跨包 import 服务端，所以协议定义必须各
自复制一份。两份的唯一判据是规格 §5（以及 `tests/contract` 的双向测试），
任何字段改动都必须先改规格。

本模块是**纯逻辑**：
- 不 import 任何 IO / asyncio / 系统时钟；
- 不持有全局可变状态；
- 所有时间（``ts_ms``）由调用方传入，便于单测精确断言。

线格式（规格 §5.1）::

    {"v": 3, "id": "<uuid4>", "t": "<type>", "ts_ms": 1730000000000, "body": {...}}

`t` 取值：
- 下行（``DOWNLINK_TYPES``）：welcome config command control ping pong uninstall update_offer
- 上行（``UPLINK_TYPES``）：hello heartbeat result event control_frame uninstall_ack pong

未知 ``t`` 必须"忽略且可观测"：解码不报错（便于调用方记日志），由链路层决定
是否丢弃；本模块只提供 ``is_known_type`` 判断。

HTTP 载体批次（``POST /api/v1/agent/link``）::

    请求  {"v": 3, "last_ack_id": "<uuid|''>", "items": [<envelope>, ...]}
    响应  {"v": 3, "ack_id": "<uuid|''>", "server_time_ms": <int>,
           "items": [<envelope>, ...]}

WS 载体：文本帧就是单个信封 JSON；二进制帧只用于实时控制画面（规格 §5.1）::

    uint32 BE 头长 | UTF-8 JSON 头 | payload
    JSON 头至少含 {"t": "ctrl", "session": "<控制会话令牌>"}

二进制帧格式与 0.2.10 的 ``pipe_proto`` 一致（同一套 framing），这样命名管道
（服务↔worker）与 WS 控制帧共用一份编解码实现。
"""

from __future__ import annotations

import json
import struct
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

#: 协议主版本。与服务端信封 ``v`` 字段一一对应；不匹配即拒收。
PROTOCOL_VERSION: Final[int] = 3

#: 服务端 -> 受控端。
DOWNLINK_TYPES: Final[frozenset[str]] = frozenset(
    {"welcome", "config", "command", "control", "ping", "pong", "uninstall", "update_offer"}
)
#: 受控端 -> 服务端。
UPLINK_TYPES: Final[frozenset[str]] = frozenset(
    {"hello", "heartbeat", "result", "event", "control_frame", "uninstall_ack", "pong"}
)
KNOWN_TYPES: Final[frozenset[str]] = DOWNLINK_TYPES | UPLINK_TYPES

#: HTTP 载体单批最多装多少个信封（服务端 hold ≤20s，批量太大会撑爆单次请求）。
BATCH_MAX_ITEMS: Final[int] = 64
#: 信封 JSON 的硬上限：超过说明 body 里塞了不该走链路的大对象（例如整份文件）。
MAX_ENVELOPE_BYTES: Final[int] = 1024 * 1024
#: 二进制帧（管道 / WS 控制帧）的头与载荷上限。
HEADER_STRUCT: Final[struct.Struct] = struct.Struct(">I")
MAX_HEADER_BYTES: Final[int] = 64 * 1024
MAX_PAYLOAD_BYTES: Final[int] = 16 * 1024 * 1024


class EnvelopeError(ValueError):
    """信封不合法（缺字段、版本不符、类型错误、超限）。"""


# --------------------------------------------------------------- 信封对象 ---
@dataclass(frozen=True, slots=True)
class Envelope:
    """一个 v3 信封。不可变，便于跨任务传递且不会被意外改写。"""

    id: str
    type: str
    ts_ms: int
    body: Mapping[str, Any]

    # -- 序列化 ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {"v": PROTOCOL_VERSION, "id": self.id, "t": self.type, "ts_ms": self.ts_ms, "body": dict(self.body)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    def to_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    # -- 便捷读 ---------------------------------------------------------
    @property
    def body_dict(self) -> dict[str, Any]:
        return dict(self.body)

    def get(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default)


def new_id() -> str:
    """生成一个幂等键（uuid4 规范字符串）。

    上行信封 id、帧 ``X-Frame-Id``、命令回执关联都用它；服务端按 id 去重，
    因此"重投安全"的前提是这个 id 在重投之间保持不变——重投时必须复用原 id，
    不能重新生成。``new_envelope`` 的 ``msg_id`` 参数就是为此保留的。
    """

    return str(uuid.uuid4())


def new_envelope(
    kind: str,
    body: Mapping[str, Any] | None = None,
    *,
    ts_ms: int,
    msg_id: str | None = None,
) -> Envelope:
    """构造一个上行信封（纯函数，时间必须由调用方给）。"""

    if not isinstance(kind, str) or not kind:
        raise EnvelopeError("信封类型必须是非空字符串")
    if not isinstance(ts_ms, int) or isinstance(ts_ms, bool) or ts_ms < 0:
        raise EnvelopeError(f"信封 ts_ms 必须是非负整数，收到 {ts_ms!r}")
    payload = dict(body or {})
    if not isinstance(payload, dict):
        raise EnvelopeError("信封 body 必须是对象")
    return Envelope(id=msg_id or new_id(), type=kind, ts_ms=ts_ms, body=payload)


def envelope_from_dict(raw: Mapping[str, Any]) -> Envelope:
    """把线上 JSON 对象还原成 ``Envelope``；任何结构问题都抛 ``EnvelopeError``。

    注意：**不校验** ``t`` 是否已知——未知类型是合法帧，规格要求忽略且可观测，
    由链路层记日志后丢弃。``v`` 必须精确等于 3，低版本/高版本都不猜。
    """

    if not isinstance(raw, Mapping):
        raise EnvelopeError(f"信封必须是对象，收到 {type(raw).__name__}")
    version = raw.get("v")
    if version != PROTOCOL_VERSION:
        raise EnvelopeError(f"协议版本不符：期望 {PROTOCOL_VERSION}，收到 {version!r}")
    msg_id = raw.get("id")
    if not isinstance(msg_id, str) or not msg_id.strip():
        raise EnvelopeError("信封缺少 id")
    kind = raw.get("t")
    if not isinstance(kind, str) or not kind.strip():
        raise EnvelopeError("信封缺少 t")
    ts_ms = raw.get("ts_ms")
    if not isinstance(ts_ms, int) or isinstance(ts_ms, bool) or ts_ms < 0:
        raise EnvelopeError(f"信封 ts_ms 非法：{ts_ms!r}")
    body = raw.get("body", {})
    if body is None:
        body = {}
    if not isinstance(body, Mapping):
        raise EnvelopeError("信封 body 必须是对象")
    return Envelope(id=msg_id, type=kind.strip(), ts_ms=ts_ms, body=dict(body))


def decode(raw: str | bytes | bytearray) -> Envelope:
    """解码单个信封（JSON 文本/字节）。"""

    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EnvelopeError(f"信封不是合法 UTF-8: {exc}") from exc
    elif isinstance(raw, str):
        text = raw
    else:  # pragma: no cover - 类型错误属于编程错误
        raise EnvelopeError(f"信封输入类型非法：{type(raw).__name__}")
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise EnvelopeError(f"信封不是合法 JSON: {exc}") from exc
    return envelope_from_dict(parsed)


def encode(envelope: Envelope) -> bytes:
    """编码单个信封；超过 1 MiB 视为协议误用（大对象走帧上传）。"""

    blob = envelope.to_bytes()
    if len(blob) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError(f"信封过大：{len(blob)} > {MAX_ENVELOPE_BYTES} 字节")
    return blob


def is_known_type(kind: str) -> bool:
    return kind in KNOWN_TYPES


def is_downlink_type(kind: str) -> bool:
    return kind in DOWNLINK_TYPES


def is_uplink_type(kind: str) -> bool:
    return kind in UPLINK_TYPES


def validate_direction(envelope: Envelope, *, downlink: bool, allow_unknown: bool = False) -> None:
    """校验信封方向；方向不符说明对端实现错，直接报错而不是猜。

    ``allow_unknown`` 用于**接收**方向：规格要求未知 ``t`` 被忽略且可观测，
    所以未知类型必须放行到链路层记日志；但已知类型走错方向仍然报错。
    """

    if allow_unknown and not is_known_type(envelope.type):
        return
    allowed = DOWNLINK_TYPES if downlink else UPLINK_TYPES
    if envelope.type not in allowed:
        raise EnvelopeError(f"信封类型 {envelope.type!r} 不属于{'下行' if downlink else '上行'}类型集合")


# --------------------------------------------------------------- 批次 ---
@dataclass(frozen=True, slots=True)
class UplinkBatch:
    """HTTP 载体的上行批次（请求体）。"""

    items: tuple[Envelope, ...] = ()
    last_ack_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "last_ack_id": self.last_ack_id,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class DownlinkBatch:
    """HTTP 载体的下行批次（响应体）。"""

    items: tuple[Envelope, ...] = ()
    ack_id: str = ""
    server_time_ms: int = 0


def build_uplink_batch(items: Iterable[Envelope], *, last_ack_id: str = "") -> dict[str, Any]:
    """构造 HTTP 载体请求体；超过 ``BATCH_MAX_ITEMS`` 直接拒绝，避免服务端截断。"""

    materialized = tuple(items)
    if len(materialized) > BATCH_MAX_ITEMS:
        raise EnvelopeError(f"单批信封过多：{len(materialized)} > {BATCH_MAX_ITEMS}")
    for item in materialized:
        validate_direction(item, downlink=False)
    return UplinkBatch(items=materialized, last_ack_id=str(last_ack_id or "")).to_dict()


def parse_downlink_batch(raw: Mapping[str, Any]) -> DownlinkBatch:
    """解析 HTTP 载体响应体；``items`` 里的单个坏帧不应毁掉整批。

    坏帧被跳过（调用方由 ``DownlinkBatch`` 无法感知——因此这里选择**严格**：
    坏帧直接抛错，让链路记一条告警并重试；静默丢下行命令比重试危险得多）。
    """

    if not isinstance(raw, Mapping):
        raise EnvelopeError("下行批次必须是对象")
    version = raw.get("v", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise EnvelopeError(f"下行批次协议版本不符：{version!r}")
    raw_items = raw.get("items", [])
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise EnvelopeError("下行批次 items 必须是数组")
    items = tuple(envelope_from_dict(item) for item in raw_items)
    for item in items:
        validate_direction(item, downlink=True, allow_unknown=True)
    ack_id = raw.get("ack_id", "")
    server_time_ms = raw.get("server_time_ms", 0)
    if not isinstance(ack_id, str):
        ack_id = ""
    if not isinstance(server_time_ms, int) or isinstance(server_time_ms, bool) or server_time_ms < 0:
        server_time_ms = 0
    return DownlinkBatch(items=items, ack_id=ack_id, server_time_ms=server_time_ms)


# ------------------------------------------------------- 二进制帧（管道） ---
def encode_binary(header: Mapping[str, Any], payload: bytes = b"") -> bytes:
    """``uint32 BE 头长 | JSON 头 | payload``。

    管道（服务↔worker）和 WS 实时控制帧共用这份实现；载荷长度写进头里的
    ``len`` 是 0.2.10 管道读端的既有约定，保留它可以让流式读端知道边界。
    """

    if not isinstance(payload, (bytes, bytearray)):
        raise EnvelopeError(f"payload 必须是 bytes，收到 {type(payload).__name__}")
    body = bytes(payload)
    if len(body) > MAX_PAYLOAD_BYTES:
        raise EnvelopeError(f"载荷过大：{len(body)} > {MAX_PAYLOAD_BYTES}")
    enriched = dict(header)
    if body:
        enriched["len"] = len(body)
    raw = json.dumps(enriched, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not raw or len(raw) > MAX_HEADER_BYTES:
        raise EnvelopeError(f"二进制帧头长度非法：{len(raw)}")
    return HEADER_STRUCT.pack(len(raw)) + raw + body


def decode_binary(blob: bytes | bytearray) -> tuple[dict[str, Any], bytes]:
    """解析二进制帧；格式错误抛 ``EnvelopeError``（不静默吞掉）。"""

    data = bytes(blob)
    if len(data) < HEADER_STRUCT.size:
        raise EnvelopeError("二进制帧截断：缺少头长度")
    (length,) = HEADER_STRUCT.unpack_from(data, 0)
    if length <= 0 or length > MAX_HEADER_BYTES:
        raise EnvelopeError(f"二进制帧头长度非法：{length}")
    start = HEADER_STRUCT.size
    end = start + length
    if len(data) < end:
        raise EnvelopeError("二进制帧截断：头不完整")
    try:
        header = json.loads(data[start:end].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EnvelopeError(f"二进制帧头不是合法 JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise EnvelopeError("二进制帧头必须是对象")
    return header, data[end:]


__all__ = [
    "BATCH_MAX_ITEMS",
    "DOWNLINK_TYPES",
    "DownlinkBatch",
    "Envelope",
    "EnvelopeError",
    "HEADER_STRUCT",
    "KNOWN_TYPES",
    "MAX_ENVELOPE_BYTES",
    "MAX_HEADER_BYTES",
    "MAX_PAYLOAD_BYTES",
    "PROTOCOL_VERSION",
    "UPLINK_TYPES",
    "UplinkBatch",
    "build_uplink_batch",
    "decode",
    "decode_binary",
    "encode",
    "encode_binary",
    "envelope_from_dict",
    "is_downlink_type",
    "is_known_type",
    "is_uplink_type",
    "new_envelope",
    "new_id",
    "parse_downlink_batch",
    "validate_direction",
]