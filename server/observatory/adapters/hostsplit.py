"""Dual-domain adapter, fail-closed (A11/A23).

Rules:
* **console paths** (everything not listed as shared) are served only for hosts
  in ``console_hosts`` — or loopback when ``allow_loopback_console`` is true;
* **shared paths** (health/version, agent link, public/install endpoints) are
  served for console hosts, agent hosts and loopback;
* every other host gets a uniform 404 — an unknown Host must never receive the
  console (old behaviour treated unknown hosts as console);
* ``X-Forwarded-Host`` is honoured only when ``trust_forwarded_for`` is enabled
  (default false, spec A11).

Incomplete configuration fails closed: if ``console_host``/``agent_hosts`` are
empty, only the loopback console (if allowed) and loopback shared paths work.
"""

from __future__ import annotations

import logging

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

SHARED_PREFIXES = (
    "/api/v1/agent/",
    "/api/v1/public/",
    "/api/v1/health",
    "/api/v1/version",
    "/install.ps1",
    "/install.cmd",
)

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
    def _console_allowed(self, host: str) -> bool:
        if host in self.console_hosts:
            return True
        return bool(self.allow_loopback_console and host in LOOPBACK_HOSTS)

    def _shared_allowed(self, host: str) -> bool:
        if host in LOOPBACK_HOSTS:
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
        path = str(scope.get("path") or "/")
        allowed = self._shared_allowed(host) if _is_shared(path) else self._console_allowed(host)
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