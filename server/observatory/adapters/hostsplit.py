"""Dual-domain adapter, fail-closed (A11/A23).

Rules:
* **console paths** (everything not listed as shared) are served only for hosts
  in ``console_hosts`` — or loopback when ``allow_loopback_console`` is true;
* **shared paths** (health/version, agent link, public/install endpoints) are
  served for console hosts, agent hosts and loopback;
* the console's two WebSockets (``/api/v1/ws``, ``/api/v1/devices/<uid>/control``)
  are shared too, **websocket scopes only** — a console domain fronted by a
  no-WebSocket CDN (Aliyun ESA, 2026-10-25) needs them on the agent domain;
  same-path REST stays console-only;
* every other host gets a uniform 404 — an unknown Host must never receive the
  console (old behaviour treated unknown hosts as console);
* ``X-Forwarded-Host`` is honoured only when ``trust_forwarded_for`` is enabled
  (default false, spec A11).

Incomplete configuration fails closed: if ``console_host``/``agent_hosts`` are
empty, only the loopback console (if allowed) and loopback shared paths work.
"""

from __future__ import annotations

import logging
import re

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: 真实对端地址的 loopback 判定集合。Starlette TestClient 的传输把 client
#: host 固定为字符串 "testclient"（非 IP）——它只存在于进程内测试传输，
#: 没有网络对端，与本机回环同等对待；此外覆盖整个 127/8。
CLIENT_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testclient"})

SHARED_PREFIXES = (
    "/api/v1/agent/",
    "/api/v1/public/",
    "/api/v1/health",
    "/api/v1/version",
    "/install.ps1",
    "/install.cmd",
)

#: 控制台页面的两条 WebSocket：实时事件流与实时控制观看端。控制台域名
#: 可能挂在不支持 WebSocket 的 CDN 后面（2026-10-25 生产：control 域换
#: 阿里云 ESA，WS 完全不可用），这两条 WS 允许在**任一**域名承载——仅限
#: websocket 连接，同路径的 REST（grant 创建/撤销）仍按控制台域隔离。
_CONTROL_WS_RE = re.compile(r"^/api/v1/devices/[^/]+/control$")
SHARED_WS_PATHS = frozenset({"/api/v1/ws"})

AGENT_ONLY_NOTICE = (
    "该域名仅用于受控端接入与安装，控制台请访问 https://{console}\n"
    "This domain only serves the agent and installer; open the console at https://{console}"
)

CLOSE_MISDIRECTED = 4403


def _normalise_host(value: str) -> str:
    host = (value or "").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.split(":", 1)[0]


def request_host(scope: Scope, *, trust_forwarded_for: bool) -> str:
    headers = dict(scope.get("headers") or [])
    if trust_forwarded_for:
        forwarded = headers.get(b"x-forwarded-host", b"").decode("latin-1")
        if forwarded:
            return _normalise_host(forwarded.split(",", 1)[0])
    return _normalise_host(headers.get(b"host", b"").decode("latin-1"))


def _is_shared(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in SHARED_PREFIXES)


def _is_shared_ws(path: str) -> bool:
    return path in SHARED_WS_PATHS or bool(_CONTROL_WS_RE.match(path))


class HostSplit:
    """ASGI middleware applying the domain policy before routing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        console_hosts: list[str],
        agent_hosts: list[str],
        trust_forwarded_for: bool = False,
        allow_loopback_console: bool = True,
    ) -> None:
        self.app = app
        self.console_hosts = [host.lower() for host in console_hosts if host]
        self.agent_hosts = [host.lower() for host in agent_hosts if host]
        self.trust_forwarded_for = trust_forwarded_for
        self.allow_loopback_console = allow_loopback_console
        self.config_complete = bool(self.console_hosts and self.agent_hosts)
        if not self.config_complete:
            log.error(
                "host split configuration is incomplete (console_hosts=%s agent_hosts=%s); "
                "failing closed: only loopback access is allowed",
                self.console_hosts,
                self.agent_hosts,
            )

    # -- policy ------------------------------------------------------------
    def _console_allowed(self, host: str, *, client_is_loopback: bool) -> bool:
        if host in self.console_hosts:
            return True
        # P0（2026-10 架构审查）：Host 头完全由客户端控制。此前公网请求只要
        # 带 ``Host: 127.0.0.1`` 就命中 loopback 分支，整个控制台 API（登录、
        # 命令、文件、删除）对互联网敞开——"fail-closed" 实为最大的 fail-open。
        # loopback 放行现在要求 **Host 是 loopback 且真实对端地址也是
        # loopback**（scope["client"]，代理后是网桥 IP、伪造不了）。
        return bool(
            self.allow_loopback_console and host in LOOPBACK_HOSTS and client_is_loopback
        )

    def _shared_allowed(self, host: str, *, client_is_loopback: bool) -> bool:
        if host in LOOPBACK_HOSTS and client_is_loopback:
            return True
        if not self.config_complete:
            return False
        return host in self.console_hosts or host in self.agent_hosts

    def _console_hostname(self) -> str:
        if self.console_hosts:
            return self.console_hosts[0]
        return "control.invalid"

    # -- ASGI --------------------------------------------------------------
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        host = request_host(scope, trust_forwarded_for=self.trust_forwarded_for)
        client_ip = (scope.get("client") or ("", 0))[0]
        client_is_loopback = _normalise_host(client_ip) in CLIENT_LOOPBACK_HOSTS or client_ip.startswith("127.")
        path = str(scope.get("path") or "/")
        allowed = (
            self._shared_allowed(host, client_is_loopback=client_is_loopback)
            if _is_shared(path)
            else self._console_allowed(host, client_is_loopback=client_is_loopback)
        )
        if not allowed and scope["type"] == "websocket" and _is_shared_ws(path):
            allowed = self._shared_allowed(host, client_is_loopback=client_is_loopback)
        if allowed:
            await self.app(scope, receive, send)
            return

        log.info("host split rejected host=%r path=%r", host, path)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": CLOSE_MISDIRECTED})
            return

        accept = dict(scope.get("headers") or []).get(b"accept", b"").decode("latin-1")
        body = self._body(wants_html="text/html" in accept)
        content_type = "text/html; charset=utf-8" if "text/html" in accept else "text/plain; charset=utf-8"
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

    def _body(self, *, wants_html: bool) -> bytes:
        message = AGENT_ONLY_NOTICE.format(console=self._console_hostname())
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
            "a{color:#6cb6ff}</style></head><body><main><h1>Observatory</h1>"
            f"<p>{escaped}</p><p><a href='https://{self._console_hostname()}'>"
            f"https://{self._console_hostname()}</a></p></main></body></html>"
        )
        return html.encode("utf-8")


def build(
    app: ASGIApp,
    *,
    console_hosts: list[str],
    agent_hosts: list[str],
    trust_forwarded_for: bool = False,
    allow_loopback_console: bool = True,
) -> HostSplit:
    """Starlette-style factory (kept for symmetry with middleware registration)."""

    return HostSplit(
        app,
        console_hosts=console_hosts,
        agent_hosts=agent_hosts,
        trust_forwarded_for=trust_forwarded_for,
        allow_loopback_console=allow_loopback_console,
    )


__all__ = [
    "AGENT_ONLY_NOTICE",
    "CLOSE_MISDIRECTED",
    "LOOPBACK_HOSTS",
    "SHARED_PREFIXES",
    "HostSplit",
    "build",
    "request_host",
]