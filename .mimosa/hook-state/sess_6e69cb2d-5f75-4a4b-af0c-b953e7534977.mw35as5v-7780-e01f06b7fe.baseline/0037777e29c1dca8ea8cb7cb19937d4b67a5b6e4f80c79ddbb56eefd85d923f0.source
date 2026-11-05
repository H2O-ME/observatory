"""Request-body size limits as an ASGI middleware (P1, 2026-10 架构审查).

uvicorn/FastAPI impose no body-size cap by themselves, and rate limiting in
the handlers only runs *after* the body has been fully parsed — an
unauthenticated client could push a multi-gigabyte ``POST /api/v1/agent/enroll``
straight into memory. This middleware refuses oversized bodies *before* the
app reads them:

* requests with a ``Content-Length`` are rejected immediately with 413;
* chunked requests (no ``Content-Length``) get a counting ``receive`` wrapper
  that **raises at the crossing read** — the app never receives the over-limit
  chunk and never learns the rest of the body exists.

S4（2026-10 独立审计）：旧实现只在包裹层记一个 ``overflowed`` 标记，继续把
超限数据喂给应用，直到应用准备响应时才改写成 413——业务副作用已经发生。
现在改成"读到这里就炸"：``receive`` 一越过阈值就抛 :class:`_BodyTooLarge`，
异常直穿上层的中间件与框架；即使某个框架把它吞掉并坚持要回自己的响应，
``send`` 拦截也会把 ``http.response.start`` 改写成统一的 413 信封（异常路径
与正常路径的载荷字节完全相同）。

Limits are per path prefix; anything unlisted falls back to a small default
(console JSON payloads). The agent-package upload keeps its own 256 MB check
inside the handler — the network-layer cap here just adds headroom above it.
"""

from __future__ import annotations

import json
import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..core.errors import BAD_REQUEST, AppError, error_payload

log = logging.getLogger(__name__)

KIB = 1024
MIB = 1024 * KIB

#: path prefix → cap in bytes (first match wins; ``None`` = use default).
AGENT_BODY_LIMITS: tuple[tuple[str, int], ...] = (
    ("/api/v1/agent/enroll", 256 * KIB),
    # The agent outbox caps the whole queue at 32 MiB; one uplink batch can
    # therefore never legitimately exceed that plus framing headroom.
    ("/api/v1/agent/link", 34 * MIB),
    # /api/v1/agent/frames is configured from settings.max_frame_bytes at
    # assembly time (see build() below) — this entry is a floor, not the cap.
)

DEFAULT_BODY_LIMIT = 2 * MIB
UPLOAD_LIMIT = 320 * MIB
UPLOAD_PREFIX = "/api/v1/updates/agent"

#: 超限错误的 message 沿用旧实现（客户端/脚本可能按文本判断），只补上
#: ``retryable`` 与 ``server_time_ms``，与 AppError 的错误信封保持一致。
_TOO_LARGE_MESSAGE = "request body too large"


def _rejection_body() -> bytes:
    """统一的 413 载荷：{code,message,retryable,server_time_ms}。

    错误码沿用既有 body-limit 约定（``BAD_REQUEST``），不新造 code——
    413 只是 HTTP 状态，不是新的业务错误类别；具体上限值只进日志，
    不改变对外的 message 文本。
    """

    error = AppError(BAD_REQUEST, _TOO_LARGE_MESSAGE, retryable=False, http_status=413)
    payload = error_payload(error)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _limit_for(path: str, *, frame_limit: int) -> int:
    for prefix, cap in AGENT_BODY_LIMITS:
        if path.startswith(prefix):
            return cap
    if path.startswith(UPLOAD_PREFIX):
        return UPLOAD_LIMIT
    if path.startswith("/api/v1/agent/frames"):
        # 帧上传是原始图像字节，自己的上限由 settings.max_frame_bytes 决定；
        # 这里只加传输层 headroom，不能套用 256 KiB 的注册上限。
        return frame_limit + 256 * KIB
    return DEFAULT_BODY_LIMIT


class BodyLimit:
    """ASGI middleware refusing oversized request bodies before parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        frame_limit: int = 6 * MIB,
        default_limit: int = DEFAULT_BODY_LIMIT,
    ) -> None:
        self.app = app
        self.frame_limit = int(frame_limit)
        self.default_limit = int(default_limit)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "/")
        limit = _limit_for(path, frame_limit=self.frame_limit)

        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                oversized = int(declared) > limit
            except ValueError:
                oversized = True
            if oversized:
                # 有声明长度时在调用应用之前就拒绝：业务处理器根本不会被执行。
                await self._reject(send, limit, path=path)
                return
            await self.app(scope, receive, send)
            return

        # 没有 Content-Length（chunked / 分块）：接收侧累计到越限的那一次
        # receive 立即抛异常，应用（FastAPI 的 request.body()、multipart 解析）
        # 在当前读取点中断，既拿不到越限数据块，也读不到后续数据。
        received = 0
        overflowed = False
        rejected = False
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received, overflowed
            if overflowed:
                # 已经判定超限：任何后续读取都立刻失败，绝不把剩余字节交给应用。
                raise _BodyTooLarge(limit)
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b"") or b"")
                if received > limit:
                    overflowed = True
                    raise _BodyTooLarge(limit)
            return message

        async def guarded_send(message: Message) -> None:
            """兜底：把应用自己的响应改写成统一 413。

            正常路径下异常会直穿回 :meth:`__call__`，根本走不到这里；
            只有在框架/应用吞掉 :class:`_BodyTooLarge` 并继续响应时才需要它。
            """

            nonlocal rejected, response_started
            if message["type"] == "http.response.start":
                if rejected:
                    return
                if overflowed:
                    rejected = True
                    await self._reject(send, limit, path=path)
                    return
                response_started = True
                await send(message)
                return
            if rejected and message["type"] == "http.response.body":
                # 已经用 413 回过话，应用后续的 body 全部丢弃（不能再写）。
                return
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except _BodyTooLarge:
            if rejected or response_started:
                # 响应已经开始（流式响应）就无法再改成 413；此时唯一正确的
                # 动作是让连接结束，日志保留证据。
                log.warning("body limit exceeded after response started (path=%s limit=%sB)", path, limit)
                return
            await self._reject(send, limit, path=path)
            return
        # 应用把异常吞掉后一声不吭地返回了：不能把客户端晾在那里。
        if overflowed and not rejected and not response_started:
            await self._reject(send, limit, path=path)

    async def _reject(self, send: Send, limit: int, *, path: str) -> None:
        log.warning("rejecting oversized body (path=%s limit=%sB)", path, limit)
        body = _rejection_body()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"cache-control", b"no-store"),
                    # 请求体没读完就回话了，连接不可复用。
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"body exceeded {limit} bytes")
        self.limit = limit


__all__ = [
    "AGENT_BODY_LIMITS",
    "BodyLimit",
    "DEFAULT_BODY_LIMIT",
    "UPLOAD_LIMIT",
    "UPLOAD_PREFIX",
]
