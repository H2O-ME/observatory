"""S4 回归：请求体超限必须在**读取时立即终止**，且回统一 413 信封。

审计现场：旧实现收到 5 MiB（限制 256 KiB 的注册路径）时把 5 MiB 全部读完、
业务处理器照常执行，直到响应阶段才把状态码改成 413。这里用最小的 ASGI
夹具复现"业务处理器是否/读了多少"这类无法只看状态码断言的事实。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from observatory.adapters.bodylimit import (
    AGENT_BODY_LIMITS,
    DEFAULT_BODY_LIMIT,
    UPLOAD_LIMIT,
    BodyLimit,
)

pytestmark = pytest.mark.unit

KIB = 1024
MIB = 1024 * KIB
ENROLL_LIMIT = dict(AGENT_BODY_LIMITS)["/api/v1/agent/enroll"]


class _Sink:
    """收集 ASGI 响应消息，供断言状态码/载荷/是否写过两个 start。"""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int:
        starts = [m for m in self.messages if m["type"] == "http.response.start"]
        assert starts, f"没有 http.response.start: {self.messages}"
        return int(starts[0]["status"])

    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")

    def json(self) -> dict[str, Any]:
        return json.loads(self.body().decode("utf-8"))


def _scope(path: str, *, content_length: int | None = None) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = [(b"host", b"localhost")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("latin-1")))
    return {"type": "http", "method": "POST", "path": path, "headers": headers}


def _chunked_receive(total: int, chunk: int = 64 * KIB, *, first_chunk: int | None = None):
    """分块 receive：模拟无 Content-Length 的流式上传，并记录被取走多少字节。"""

    state = {"sent": 0, "calls": 0}
    head = chunk if first_chunk is None else min(first_chunk, total)

    async def receive() -> dict[str, Any]:
        state["calls"] += 1
        sent = state["sent"]
        if sent >= total:
            return {"type": "http.request", "body": b"", "more_body": False}
        size = head if sent == 0 else min(chunk, total - sent)
        size = min(size, total - sent)
        state["sent"] = sent + size
        return {
            "type": "http.request",
            "body": b"x" * size,
            "more_body": state["sent"] < total,
        }

    return receive, state


def _run(app, scope, receive, sink) -> None:
    asyncio.run(BodyLimit(app, frame_limit=6 * MIB)(scope, receive, sink))


def _assert_413(sink: _Sink) -> None:
    assert sink.status == 413
    payload = sink.json()
    assert payload["code"] == "BAD_REQUEST", "错误码必须沿用既有 body-limit 约定"
    assert payload["message"] == "request body too large"
    assert payload["retryable"] is False
    assert "server_time_ms" in payload, "必须是统一错误信封，而不是半个 JSON"


def test_declared_content_length_is_rejected_before_handler() -> None:
    """声明长度超限：应用（业务处理器）根本不该被调用。"""

    calls = {"handler": 0}

    async def app(scope, receive, send) -> None:  # noqa: ANN001 - ASGI 夹具
        calls["handler"] += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    sink = _Sink()
    receive, _state = _chunked_receive(5 * MIB)
    _run(app, _scope("/api/v1/agent/enroll", content_length=5 * MIB), receive, sink)

    _assert_413(sink)
    assert calls["handler"] == 0, "超限请求不得进入业务处理器"


def test_chunked_overflow_aborts_at_read_time() -> None:
    """分块超限：异常在越限的那一次 receive 抛出，应用读不到任何字节。"""

    observed: dict[str, Any] = {"handler": 0, "read": 0, "business": False, "caught": ""}

    async def app(scope, receive, send) -> None:  # noqa: ANN001 - ASGI 夹具
        observed["handler"] += 1
        # 模拟 FastAPI request.body()：循环读取直到 more_body 结束。
        try:
            while True:
                message = await receive()
                observed["read"] += len(message.get("body", b"") or b"")
                if not message.get("more_body", False):
                    break
        except BaseException as exc:  # noqa: BLE001 - 夹具需要知道异常确实上抛
            observed["caught"] = type(exc).__name__
            raise
        # 只有完整读到 body 才会走到业务：这里绝不能被执行。
        observed["business"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    sink = _Sink()
    # 一次 5 MiB 的块：旧实现会读满 5 MiB；新实现第一个越限块就不交给应用。
    receive, state = _chunked_receive(5 * MIB, first_chunk=5 * MIB)
    _run(app, _scope("/api/v1/agent/enroll"), receive, sink)

    _assert_413(sink)
    assert observed["handler"] == 1, "chunked 无法预知长度：应用被进入是 ASGI 事实"
    assert observed["read"] == 0, f"超限块不得交给应用读取，实际读了 {observed['read']} 字节"
    assert observed["business"] is False, "业务副作用绝不能发生"
    assert observed["caught"] == "_BodyTooLarge", "必须以明确异常终止，而不是打标记继续"
    assert state["calls"] == 1, "越限后不得继续向客户端要数据"


def test_chunked_overflow_after_partial_reads_stops_early() -> None:
    """小分块场景：应用最多读到阈值附近就必须中断，不会读完整缓冲。"""

    observed = {"read": 0, "business": False}

    async def app(scope, receive, send) -> None:  # noqa: ANN001 - ASGI 夹具
        while True:
            message = await receive()
            observed["read"] += len(message.get("body", b"") or b"")
            if not message.get("more_body", False):
                break
        observed["business"] = True

    sink = _Sink()
    receive, state = _chunked_receive(4 * MIB, chunk=64 * KIB)
    _run(app, _scope("/api/v1/agent/enroll"), receive, sink)

    _assert_413(sink)
    assert observed["business"] is False
    assert observed["read"] <= ENROLL_LIMIT
    assert state["sent"] <= ENROLL_LIMIT + 64 * KIB, "不得把 4 MiB 全部读进内存"


def test_framework_swallowing_the_abort_still_gets_413_envelope() -> None:
    """框架吞掉异常仍回自己的响应：send 拦截必须改写成同一份 413。"""

    async def app(scope, receive, send) -> None:  # noqa: ANN001 - ASGI 夹具
        try:
            await receive()
        except Exception:  # noqa: BLE001 - 模拟框架的宽捕获
            pass
        # 应用仍然坚持回 200：客户端绝不能看到这个响应。
        await send({"type": "http.response.start", "status": 200, "headers": [(b"x-mode", b"swallowed")]})
        await send({"type": "http.response.body", "body": b'{"business":"side-effect"}'})

    sink = _Sink()
    receive, _state = _chunked_receive(5 * MIB, first_chunk=5 * MIB)
    _run(app, _scope("/api/v1/agent/enroll"), receive, sink)

    _assert_413(sink)
    starts = [m for m in sink.messages if m["type"] == "http.response.start"]
    assert len(starts) == 1, "只能有一个 response.start（不能先 200 再 413）"
    assert b"side-effect" not in sink.body()


def test_silently_swallowed_abort_still_answers_413() -> None:
    """应用吞掉异常后直接返回、连响应都不写：中间件必须补上 413，不能挂住客户端。"""

    async def app(scope, receive, send) -> None:  # noqa: ANN001 - ASGI 夹具
        try:
            await receive()
        except Exception:  # noqa: BLE001 - 模拟宽捕获
            return

    sink = _Sink()
    receive, _state = _chunked_receive(5 * MIB, first_chunk=5 * MIB)
    _run(app, _scope("/api/v1/agent/enroll"), receive, sink)

    _assert_413(sink)


def test_normal_sized_request_passes_through_unchanged() -> None:
    """正常请求不能被误伤：应用拿到完整 body 并正常响应。"""

    seen: dict[str, Any] = {"handler": 0, "body": b""}

    async def app(scope, receive, send) -> None:  # noqa: ANN001 - ASGI 夹具
        seen["handler"] += 1
        while True:
            message = await receive()
            seen["body"] += message.get("body", b"") or b""
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    sink = _Sink()
    receive, _state = _chunked_receive(100 * KIB, chunk=16 * KIB)
    _run(app, _scope("/api/v1/agent/enroll"), receive, sink)

    assert sink.status == 200
    assert seen["handler"] == 1
    assert seen["body"] == b"x" * (100 * KIB)

    # 有声明长度且未超限：同样放行。
    sink = _Sink()
    receive, _state = _chunked_receive(100 * KIB, chunk=100 * KIB)
    _run(app, _scope("/api/v1/agent/enroll", content_length=100 * KIB), receive, sink)
    assert sink.status == 200


def test_path_specific_limits_are_preserved() -> None:
    """上传/帧/默认路径各自的上限不能被注册路径的 256 KiB 一起卡住。"""

    async def app(scope, receive, send) -> None:  # noqa: ANN001 - ASGI 夹具
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    # 安装包上传：50 MiB 远高于默认值，但在独立上限（320 MiB）之内。
    sink = _Sink()
    _run(app, _scope("/api/v1/updates/agent", content_length=50 * MIB), _chunked_receive(0)[0], sink)
    assert sink.status == 200
    assert 50 * MIB < UPLOAD_LIMIT

    # 帧上传：6 MiB + 1 KiB 超过 max_frame_bytes 本身，但在传输层 headroom 内。
    sink = _Sink()
    _run(app, _scope("/api/v1/agent/frames", content_length=6 * MIB + KIB), _chunked_receive(0)[0], sink)
    assert sink.status == 200

    # 帧上传超过 headroom 仍然要拒。
    sink = _Sink()
    _run(app, _scope("/api/v1/agent/frames", content_length=7 * MIB), _chunked_receive(0)[0], sink)
    assert sink.status == 413

    # 未列出的控制台 JSON 路径用默认上限。
    sink = _Sink()
    _run(app, _scope("/api/v1/settings", content_length=DEFAULT_BODY_LIMIT + 1), _chunked_receive(0)[0], sink)
    assert sink.status == 413

    # 非法 Content-Length 视为超限（不静默放行）。
    sink = _Sink()
    bad_headers = [(b"content-length", b"not-a-number")]
    _run(
        app,
        {"type": "http", "method": "POST", "path": "/api/v1/agent/enroll", "headers": bad_headers},
        _chunked_receive(0)[0],
        sink,
    )
    assert sink.status == 413
