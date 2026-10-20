"""Framing shared by the named pipe and the WebSocket channel.

Identical wire format to the server's ``observatory.framing``:

    uint32 big-endian header length | UTF-8 JSON header | payload

Keeping one format means a captured frame looks the same to the pipe reader and to
the WebSocket writer, so there is no second implementation to keep in sync.
"""

from __future__ import annotations

import json
import struct

HEADER_STRUCT = struct.Struct(">I")
MAX_HEADER_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


class FramingError(ValueError):
    pass


def encode(header: dict, payload: bytes = b"") -> bytes:
    raw = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_HEADER_BYTES:
        raise FramingError("header too large")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise FramingError("payload too large")
    return HEADER_STRUCT.pack(len(raw)) + raw + payload


def decode(blob: bytes) -> tuple[dict, bytes]:
    if len(blob) < HEADER_STRUCT.size:
        raise FramingError("truncated header length")
    (length,) = HEADER_STRUCT.unpack_from(blob, 0)
    if length <= 0 or length > MAX_HEADER_BYTES:
        raise FramingError(f"invalid header length {length}")
    start = HEADER_STRUCT.size
    end = start + length
    if len(blob) < end:
        raise FramingError("truncated header")
    header = json.loads(blob[start:end].decode("utf-8"))
    if not isinstance(header, dict):
        raise FramingError("header must be an object")
    return header, blob[end:]


def read_exact(handle, size: int, *, peek, sleep) -> bytes | None:
    """Read ``size`` bytes, polling with ``peek`` so a synchronous pipe handle
    never blocks forever when the peer disappears."""

    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        available = peek()
        if available <= 0:
            sleep(0.02)
            continue
        chunk = handle.read(min(remaining, available))
        if not chunk:
            sleep(0.02)
            continue
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


__all__ = [
    "FramingError",
    "HEADER_STRUCT",
    "MAX_HEADER_BYTES",
    "MAX_PAYLOAD_BYTES",
    "decode",
    "encode",
    "read_exact",
]