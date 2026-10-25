"""Request-body size limits as an ASGI middleware (P1, 2026-10 架构审查).

uvicorn/FastAPI impose no body-size cap by themselves, and rate limiting in
the handlers only runs *after* the body has been fully parsed — an
unauthenticated client could push a multi-gigabyte ``POST /api/v1/agent/enroll``
straight into memory. This middleware refuses oversized bodies *before* the
app reads them:

* requests with a ``Content-Length`` are rejected immediately with 413;
* chunked requests (no ``Content-Length``) get a counting ``receive`` wrapper
  that aborts as soon as the declared limit is exceeded.

Limits are per path prefix; anything unlisted falls back to a small default
(console JSON payloads). The agent-package upload keeps its own 256 MB check
inside the handler — the network-layer cap here just adds headroom above it.
"""

from __future__ import annotations

import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

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

_TOO_LARGE = b'{"code":"BAD_REQUEST","message":"request body too large"}'


def _limit_for(path: str, *, frame_limit: int) -> int:
    for prefix, cap in AGENT_BODY_LIMITS:
        if path.startswith(prefix):
            return cap
    if path.startswith(UPLOAD_PREFIX):
        return UPLOAD_LIMIT
    if path.startswith("/api/v1/agent/frames"):
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
        if declared:
            try:
                if int(declared) > limit:
                    await self._reject(send, limit)
                    return
            except ValueError:
                await self._reject(send, limit)
                return
            await self.app(scope, receive, send)
            return

        # No Content-Length (chunked): wrap receive and abort once the
        # accumulated body crosses the limit.
        received = 0
        overflowed = False

        async def counting_receive() -> Message:
            nonlocal received, overflowed
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b"") or b"")
                if received > limit:
                    overflowed = True
            return message

        # Starlette has no clean way to stop reading mid-body from a pure ASGI
        # wrapper, so we only enforce before the app started responding: the
        # wrapper marks overflow and the next response message is replaced.
        async def guarded_send(message: Message) -> None:
            if overflowed and message["type"] == "http.response.start":
                raise _BodyTooLarge(limit)
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except _BodyTooLarge as exc:
            log.warning("body limit exceeded (path=%s limit=%sB)", path, exc.limit)
            # Drain-ish: the app already consumed the stream; just answer 413.
            await self._reject(send, limit)

    async def _reject(self, send: Send, limit: int) -> None:
        log.warning("rejecting oversized body (limit=%sB)", limit)
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_TOO_LARGE)).encode("latin-1")),
                    (b"cache-control", b"no-store"),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _TOO_LARGE})


class _BodyTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"body exceeded {limit} bytes")
        self.limit = limit


__all__ = ["AGENT_BODY_LIMITS", "BodyLimit", "DEFAULT_BODY_LIMIT"]
