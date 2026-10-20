"""Binary envelope: ``uint32 header length | JSON header | payload``.

The same framing is used by the agent's WebSocket and its local named pipe, so a
frame or an input event is parsed by one shared implementation on both sides.
"""

from __future__ import annotations

import json
import struct

HEADER_STRUCT = struct.Struct(">I")
MAX_HEADER_BYTES = 64 * 1024


class FramingError(ValueError):
    pass


def encode(header: dict, payload: bytes = b"") -> bytes:
    raw = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_HEADER_BYTES:
        raise FramingError("header too large")
    return HEADER_STRUCT.pack(len(raw)) + raw + payload


def decode_header_length(data: bytes) -> int:
    if len(data) < HEADER_STRUCT.size:
        raise FramingError("truncated header length")
    (length,) = HEADER_STRUCT.unpack_from(data, 0)
    if length <= 0 or length > MAX_HEADER_BYTES:
        raise FramingError(f"invalid header length {length}")
    return length


def decode(blob: bytes) -> tuple[dict, bytes]:
    length = decode_header_length(blob)
    start = HEADER_STRUCT.size
    end = start + length
    if len(blob) < end:
        raise FramingError("truncated header")
    header = json.loads(blob[start:end].decode("utf-8"))
    if not isinstance(header, dict):
        raise FramingError("header must be an object")
    return header, blob[end:]


__all__ = ["FramingError", "MAX_HEADER_BYTES", "decode", "decode_header_length", "encode"]