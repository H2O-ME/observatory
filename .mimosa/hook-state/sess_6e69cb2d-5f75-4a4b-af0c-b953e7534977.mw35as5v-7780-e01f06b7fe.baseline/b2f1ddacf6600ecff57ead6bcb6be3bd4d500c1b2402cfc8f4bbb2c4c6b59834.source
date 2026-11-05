"""A1：管道分片限制——大 body 走二进制载荷，双向完整文件哈希一致。

背景（审计 A1）：``core/envelope.encode_binary`` 的 JSON 头硬上限 64 KiB，而
``runtime`` 把文件分片 base64 后塞进头里；前端每片 1 MiB、受控端读 700 KiB，
于是 **正常文件既传不上去也传不下来**（32 KiB 成功，64 KiB 起
``PipeProtocolError``）。修复：超水位时 body 外置到二进制载荷，内联格式保持
向后兼容。
"""

from __future__ import annotations

import hashlib
import json

import pytest

pytestmark = pytest.mark.unit


def _envelope():
    from observatory_agent.core import envelope  # noqa: PLC0415

    return envelope


def _pipe():
    from observatory_agent.win import pipe  # noqa: PLC0415

    return pipe


# ------------------------------------------------------------------ codec ---
def test_small_message_stays_inline() -> None:
    """小消息保持旧的内联格式（向后兼容：旧读端还能用）。"""

    envelope = _envelope()
    wire = envelope.encode_binary({"v": 3, "id": "x", "t": "result", "body": {"ok": True}})
    header, payload = envelope.decode_binary(wire)
    assert header["body"] == {"ok": True}
    assert payload == b""
    assert envelope.BODY_IN_PAYLOAD not in header


def test_large_body_spills_to_payload_and_roundtrips() -> None:
    """A1 的核心：64 KiB 以上的 body 不再抛错，而是外置后无损还原。"""

    envelope = _envelope()
    big = "A" * (200 * 1024)
    message = {"v": 3, "id": "big-1", "t": "result", "body": {"data": big, "ok": True}}
    wire = envelope.encode_binary(message)
    # 头必须留在硬上限内（这是旧实现的崩溃点）。
    head_len = envelope.HEADER_STRUCT.unpack_from(wire, 0)[0]
    assert head_len <= envelope.MAX_HEADER_BYTES
    header, payload = envelope.decode_binary(wire)
    assert payload == b""
    assert header["body"] == {"data": big, "ok": True}
    assert envelope.BODY_IN_PAYLOAD not in header, "还原后不应泄漏内部标记"


def test_full_chunk_sizes_no_longer_raise() -> None:
    """审计实测的失败点逐个回归：64 KiB / 700 KiB / 1 MiB 分片都必须能过。"""

    envelope = _envelope()
    for raw_size in (32 * 1024, 64 * 1024, 700 * 1024, 1024 * 1024):
        raw = bytes(range(256)) * (raw_size // 256)
        import base64  # noqa: PLC0415

        body = {"op": "read", "data": base64.b64encode(raw).decode("ascii")}
        wire = envelope.encode_binary({"v": 3, "id": f"chunk-{raw_size}", "t": "result", "body": body})
        header, _payload = envelope.decode_binary(wire)
        assert base64.b64decode(header["body"]["data"]) == raw, raw_size


def test_oversize_body_beyond_payload_cap_is_clear_error() -> None:
    """真超过 16 MiB 载荷上限时给**明确**错误，不是崩溃或静默截断。"""

    envelope = _envelope()
    huge = "A" * (envelope.MAX_PAYLOAD_BYTES + 1024)
    with pytest.raises(envelope.EnvelopeTooLarge) as info:
        envelope.encode_binary({"v": 3, "id": "huge", "t": "result", "body": {"data": huge}})
    assert "过大" in str(info.value)


def test_oversize_header_without_body_is_clear_error() -> None:
    """头本身就超限且没有 body 可外置 → 明确错误（画面帧不能走外置）。"""

    envelope = _envelope()
    bloated = {f"k{index}": "v" * 200 for index in range(600)}
    bloated["t"] = "frame"
    with pytest.raises(envelope.EnvelopeTooLarge):
        envelope.encode_binary(bloated)


# --------------------------------------------------------------- pipe layer ---
def _fake_stream(blob: bytes):
    """把一段字节流包装成 ``read_packet`` 需要的 peek/read/sleep/stop。"""

    state = {"pos": 0}

    def peek() -> int | None:
        return len(blob) - state["pos"]

    def read(size: int) -> bytes | None:
        chunk = blob[state["pos"] : state["pos"] + size]
        state["pos"] += len(chunk)
        return chunk or None

    def sleep(_seconds: float) -> None:
        return None

    return peek, read, sleep, (lambda: False)


def test_pipe_read_packet_restores_spilled_body() -> None:
    """``read_packet`` 不走 decode_binary，必须自己还原外置 body（A1）。"""

    pipe = _pipe()
    big = "B" * (300 * 1024)
    wire = pipe.encode_message(
        {"v": 3, "id": "spill", "t": "command", "body": {"payload": {"data": big}}}
    )
    peek, read, sleep, stop = _fake_stream(wire)
    packet = pipe.read_packet(peek=peek, read=read, sleep=sleep, stop=stop)
    assert packet is not None
    header, payload = packet
    assert payload == b""
    assert header["body"]["payload"]["data"] == big


def test_pipe_frame_keeps_stream_aligned() -> None:
    """外置帧之后紧跟的下一帧仍能被正确切分（长度字段没被 body 吃掉）。"""

    pipe = _pipe()
    big = "C" * (300 * 1024)
    first = pipe.encode_message({"v": 3, "id": "a", "t": "result", "body": {"data": big}})
    second = pipe.encode_message({"v": 3, "id": "b", "t": "result", "body": {"ok": True}})
    peek, read, sleep, stop = _fake_stream(first + second)
    assert pipe.read_packet(peek=peek, read=read, sleep=sleep, stop=stop) is not None
    tail = pipe.read_packet(peek=peek, read=read, sleep=sleep, stop=stop)
    assert tail is not None
    assert tail[0]["body"] == {"ok": True}


def test_pipe_too_large_is_recoverable_not_protocol_error() -> None:
    """超限必须与"流损坏"分开：可恢复错误 → 明确失败回执，而不是断链重启。"""

    envelope = _envelope()
    pipe = _pipe()
    huge = "D" * (envelope.MAX_PAYLOAD_BYTES + 4096)
    with pytest.raises(pipe.PipePayloadTooLarge):
        pipe.encode_message({"v": 3, "id": "huge", "t": "result", "body": {"data": huge}})
    # 它仍然是 PipeProtocolError 的子类（旧的宽捕获路径不会漏掉）。
    assert issubclass(pipe.PipePayloadTooLarge, pipe.PipeProtocolError)
    # 同时是平台无关的 EnvelopeTooLarge，runtime 的 worker 发送路径靠它判定。
    assert issubclass(pipe.PipePayloadTooLarge, envelope.EnvelopeTooLarge)


# ------------------------------------------------------- end-to-end hashing ---
def test_upload_chunks_roundtrip_hash(tmp_dir) -> None:
    """**上传方向**：分片 base64 过管道 → 落盘 → 整文件 sha256 与原文件一致。"""

    from observatory_agent.tasks import fs  # noqa: PLC0415
    from observatory_agent.win import pipe  # noqa: PLC0415

    original = bytes(range(256)) * 4096  # 1 MiB，覆盖前端单片的完整尺寸
    source = tmp_dir / "upload-src.bin"
    source.write_bytes(original)
    target = tmp_dir / "upload-dst.bin"
    chunk = pipe.MAX_PAYLOAD_BYTES  # 用远大于旧 64 KiB 头的分片装载
    offset = 0
    while offset < len(original):
        piece = original[offset : offset + chunk]
        body = {
            "command_id": f"write-{offset}",
            "type": "files",
            "payload": {
                "op": "write",
                "path": str(target),
                "offset": offset,
                "truncate": offset == 0,
                "data": __import__("base64").b64encode(piece).decode("ascii"),
            },
            "deadline_ms": 90_000,
        }
        wire = pipe.encode_message({"v": 3, "id": f"w{offset}", "t": "command", "body": body})
        peek, read, sleep, stop = _fake_stream(wire)
        header, _payload = pipe.read_packet(peek=peek, read=read, sleep=sleep, stop=stop)
        command = header["body"]
        fs.perform(
            "write",
            command["payload"],
            allow_write=True,
        )
        offset += len(piece)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(original).hexdigest()
    assert fs.perform("hash", {"path": str(target)})["data"]["sha256"] == hashlib.sha256(original).hexdigest()


def test_download_chunks_roundtrip_hash(tmp_dir) -> None:
    """**下载方向**：分片读出来 → 过管道回执 → 拼回文件 → 整文件哈希一致。"""

    import base64  # noqa: PLC0415

    from observatory_agent.tasks import fs  # noqa: PLC0415
    from observatory_agent.win import pipe  # noqa: PLC0415

    original = bytes(range(256)) * 4096  # 1 MiB
    source = tmp_dir / "download-src.bin"
    source.write_bytes(original)
    collected = bytearray()
    offset = 0
    while offset < len(original):
        result = fs.perform("read", {"path": str(source), "offset": offset, "length": fs.MAX_READ_BYTES})
        body = {
            "command_id": f"read-{offset}",
            "ok": True,
            "result": result,
            "error": None,
            "error_code": None,
        }
        # 关键回归：700 KiB 分片的 base64 超过 64 KiB，旧实现在这里抛
        # PipeProtocolError（数据传不下来）。
        wire = pipe.encode_message({"v": 3, "id": f"r{offset}", "t": "result", "body": body})
        peek, read, sleep, stop = _fake_stream(wire)
        header, _payload = pipe.read_packet(peek=peek, read=read, sleep=sleep, stop=stop)
        data = header["body"]["result"]["data"]
        collected += base64.b64decode(data["data"])
        if data["eof"]:
            break
        offset += int(data["length"])
    assert bytes(collected) == original
    assert hashlib.sha256(collected).hexdigest() == hashlib.sha256(original).hexdigest()


def test_spilled_body_marked_len_matches_payload() -> None:
    """外置帧的 ``len`` 必须等于载荷长度（流式读端靠它切边界）。"""

    envelope = _envelope()
    big = "E" * (150 * 1024)
    wire = envelope.encode_binary({"v": 3, "id": "len", "t": "result", "body": {"data": big}})
    head_len = envelope.HEADER_STRUCT.unpack_from(wire, 0)[0]
    header = json.loads(wire[4 : 4 + head_len].decode("utf-8"))
    assert header["len"] == len(wire) - 4 - head_len


# ------------------------------------------------- 端到端上限一致性（A1） ---
def test_read_result_envelope_stays_under_link_limit() -> None:
    """受控端 700 KiB 读上限 + base64 膨胀后，回执信封必须仍在 1 MiB 链路上限内。

    这是 A1 的另一半要求："统一端到端分片限制"。管道问题已由外置 body 解决，
    但 ``link._http_round`` 仍会丢弃 >1 MiB 的上行条目（服务端整批 422 会连坐
    64 条无关回执），所以这个余量必须被断言住，不能只靠注释。
    """

    import base64  # noqa: PLC0415

    from observatory_agent.core import envelope as envelope_module  # noqa: PLC0415
    from observatory_agent.core.commands import CommandResult  # noqa: PLC0415
    from observatory_agent.tasks import fs  # noqa: PLC0415

    raw = bytes(range(256)) * (fs.MAX_READ_BYTES // 256)
    payload = {
        "ok": True,
        "op": "read",
        "data": {
            "offset": 0,
            "length": len(raw),
            "size": len(raw),
            "eof": True,
            "data": base64.b64encode(raw).decode("ascii"),
            "server_time_ms": 1_700_000_000_000,
        },
        "error": None,
        "elapsed_ms": 12,
    }
    body = CommandResult(ok=True, result=payload).to_body(
        "11111111-2222-3333-4444-555555555555",
        device_time_ms=1_700_000_000_000,
        elapsed_ms=12,
    )
    wire = envelope_module.new_envelope("result", body, ts_ms=1_700_000_000_000)
    size = len(envelope_module.encode(wire))
    assert size < envelope_module.MAX_ENVELOPE_BYTES, f"回执 {size}B 超过链路信封上限"
    # 也要能过管道（外置后）。
    pipe = _pipe()
    assert pipe.encode_message(wire)  # 不抛即通过


def test_write_command_fits_pipe_for_full_frontend_chunk() -> None:
    """前端 1 MiB 分片（base64 后 ~1.33 MiB）作为下行命令必须能过管道。"""

    import base64  # noqa: PLC0415

    pipe = _pipe()
    raw = bytes(range(256)) * 4096  # 前端 CHUNK_BYTES = 1 MiB
    command = {
        "v": 3,
        "id": "write-1",
        "t": "command",
        "body": {
            "command_id": "c1",
            "type": "files",
            "deadline_ms": 90_000,
            "payload": {
                "op": "write",
                "path": r"C:\tmp\x.bin",
                "offset": 0,
                "truncate": True,
                "data": base64.b64encode(raw).decode("ascii"),
            },
        },
    }
    wire = pipe.encode_message(command)
    peek, read, sleep, stop = _fake_stream(wire)
    header, payload = pipe.read_packet(peek=peek, read=read, sleep=sleep, stop=stop)
    assert payload == b""
    assert header["body"]["payload"]["data"] == command["body"]["payload"]["data"]


# ------------------------------------------------- 失败必须可观测（A1） ---
class _OversizePipe:
    """``send`` 永远抛 PipePayloadTooLarge（模拟"这条消息装不进一帧"）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.connected = type("E", (), {"is_set": lambda self: True})()

    def send(self, envelope, payload: bytes = b"") -> None:
        from observatory_agent.win.pipe import PipePayloadTooLarge  # noqa: PLC0415

        self.calls += 1
        raise PipePayloadTooLarge("载荷过大：999 > 16")


class _RecordThenOversizePipe:
    """第一帧记录后抛超限，用来观察"补发的最小失败回执"。"""

    def __init__(self) -> None:
        self.envelopes: list[dict] = []
        self.connected = type("E", (), {"is_set": lambda self: True})()

    def send(self, envelope, payload: bytes = b"") -> None:
        from observatory_agent.win.pipe import PipePayloadTooLarge  # noqa: PLC0415

        self.envelopes.append(envelope.body_dict)
        raise PipePayloadTooLarge("载荷过大：999 > 16")


def test_worker_result_overflow_returns_clear_failure() -> None:
    """worker 回执装不下时补发**明确失败回执**，且 worker 不退出（A1）。

    旧实现把所有发送异常当作断链并 ``self._stop.set()``：一条超大的 files 回执
    就能让整个用户会话的 worker 退出，服务端只能等命令超时。
    """

    from observatory_agent.core.commands import ERROR_BAD_REQUEST, CommandResult  # noqa: PLC0415
    from observatory_agent.core.runtime import WorkerRuntime  # noqa: PLC0415

    worker = WorkerRuntime(session_id=11)
    pipe = _RecordThenOversizePipe()
    worker._pipe = pipe  # noqa: SLF001
    worker._send_result("cmd-1", CommandResult(ok=True, result={"data": "x" * 100}))  # noqa: SLF001

    assert worker._stop.is_set() is False, "超限绝不能终止 worker"
    assert len(pipe.envelopes) == 2, "必须补发一条最小失败回执"
    fallback = pipe.envelopes[1]
    assert fallback["ok"] is False
    assert fallback["error_code"] == ERROR_BAD_REQUEST
    assert fallback["command_id"] == "cmd-1"
    assert "分片" in (fallback["error"] or "")


def test_worker_send_envelope_overflow_does_not_stop_worker() -> None:
    from observatory_agent.core.runtime import WorkerRuntime  # noqa: PLC0415

    worker = WorkerRuntime(session_id=12)
    worker._pipe = _OversizePipe()  # noqa: SLF001
    ok = worker._send_envelope("result", {"ok": True})  # noqa: SLF001
    assert ok is False
    assert worker._stop.is_set() is False, "PipePayloadTooLarge 是可恢复错误，不该停 worker"


def test_service_pipe_failure_maps_oversize_to_bad_request() -> None:
    """服务进程转发命令时超限 → 回 BAD_REQUEST + 可读原因（不是断链错误）。"""

    from observatory_agent.core.commands import ERROR_BAD_REQUEST  # noqa: PLC0415
    from observatory_agent.core.runtime import _pipe_failure  # noqa: PLC0415
    from observatory_agent.win.pipe import PipePayloadTooLarge  # noqa: PLC0415

    fields = _pipe_failure(PipePayloadTooLarge("载荷过大：99999 > 16777216"))
    assert fields["error_code"] == ERROR_BAD_REQUEST
    assert "减小分片" in fields["error"]
    # 真正的断链仍按原来的笼统错误处理。
    plain = _pipe_failure(ConnectionError("pipe closed"))
    assert "error_code" not in plain
    assert "管道已断开" in plain["error"]
