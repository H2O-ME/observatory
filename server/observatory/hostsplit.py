"""Two-domain policy enforced in the application, not only in nginx.

``control.tianhw.top`` serves the console. ``observatory.123423.xyz`` exists so a
machine that cannot reach the first domain can still enroll, fetch the install
script and upload frames — but it must never expose the console.

nginx already splits the vhosts, but nginx is outside this repository's control on
the production host and the ``Host`` header is attacker-controlled, so the rule is
enforced here as well. Unknown hosts (direct IP access, local development,
``127.0.0.1``) are treated as the console: the split only ever *restricts* hosts
that are explicitly listed as agent-only.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

#: Paths any domain may serve: device-facing traffic and the install script.
AGENT_PREFIXES = ("/api/v1/agent/", "/api/v1/public/")
SHARED_EXACT = frozenset(
    {
        "/api/v1/health",
        "/api/v1/version",
        "/install.ps1",
        "/install",
        "/install/",
        "/favicon.ico",
    }
)

AGENT_ONLY_NOTICE = (
    "该域名仅用于受控端接入与安装，控制台请访问 {console}。\n"
    "This domain only serves the agent and installer; open the console at {console}."
)


def _normalise_host(value: str) -> str:
    host = (value or "").strip().lower()
    if host.startswith("["):  # IPv6 literal
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.split(":", 1)[0]


def normalise_host(value: str) -> str:
    return _normalise_host(value)


def request_host(scope: Scope) -> str:
    headers = dict(scope.get("headers") or [])
    forwarded = headers.get(b"x-forwarded-host", b"").decode("latin-1")
    if forwarded:
        return _normalise_host(forwarded.split(",", 1)[0])
    return _normalise_host(headers.get(b"host", b"").decode("latin-1"))


class HostSplit:
    def __init__(self, app: ASGIApp, *, console_hosts: list[str], agent_hosts: list[str]) -> None:
        self.app = app
        self.console_hosts = [host.lower() for host in console_hosts if host]
        self.agent_only = [
            host.lower() for host in agent_hosts if host and host.lower() not in self.console_hosts
        ]
        self.enabled = bool(self.console_hosts and self.agent_only)
        if self.enabled:
            log.info("host split active: console=%s agent-only=%s", self.console_hosts, self.agent_only)

    def _is_agent_only(self, host: str) -> bool:
        if not self.enabled:
            return False
        if host in self.console_hosts:
            return False
        return host in self.agent_only

    @staticmethod
    def _is_shared(path: str) -> bool:
        if path in SHARED_EXACT:
            return True
        return any(path.startswith(prefix) for prefix in AGENT_PREFIXES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        host = request_host(scope)
        path = scope.get("path", "/")
        if not self._is_agent_only(host) or self._is_shared(path):
            await self.app(scope, receive, send)
            return

        console = self.console_hosts[0] if self.console_hosts else "control.tianhw.top"
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return

        accept = dict(scope.get("headers") or []).get(b"accept", b"").decode("latin-1")
        wants_html = "text/html" in accept
        body = self._body(console, wants_html)
        content_type = "text/html; charset=utf-8" if wants_html else "text/plain; charset=utf-8"
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [
                    (b"content-type", content_type.encode("latin-1")),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _body(console: str, wants_html: bool) -> bytes:
        message = AGENT_ONLY_NOTICE.format(console=console)
        if not wants_html:
            return message.encode("utf-8")
        escaped = message.replace("\n", "<br>")
        html = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Observatory</title>"
            "<style>body{margin:0;min-height:100dvh;display:grid;place-items:center;"
            "font:16px/1.7 system-ui,sans-serif;background:#0b0f14;color:#e6edf3;padding:24px}"
            "main{max-width:32rem}h1{font-size:1.1rem;margin:0 0 .75rem;color:#8b98a5}"
            ".big{font-size:1.35rem;font-weight:600;margin:0 0 1rem}a{color:#6cb6ff}</style>"
            f"</head><body><main><h1>Observatory</h1><p class='big'>{escaped}</p>"
            f"<p><a href='https://{console}'>https://{console}</a></p></main></body></html>"
        )
        return html.encode("utf-8")


def build(console_hosts: list[str], agent_hosts: list[str]) -> Any:
    """Middleware factory (Starlette style)."""

    def factory(app: ASGIApp) -> HostSplit:
        return HostSplit(app, console_hosts=console_hosts, agent_hosts=agent_hosts)

    return factory


_ = (Message, Any)

__all__ = ["AGENT_ONLY_NOTICE", "AGENT_PREFIXES", "HostSplit", "SHARED_EXACT", "build", "normalise_host", "request_host"]