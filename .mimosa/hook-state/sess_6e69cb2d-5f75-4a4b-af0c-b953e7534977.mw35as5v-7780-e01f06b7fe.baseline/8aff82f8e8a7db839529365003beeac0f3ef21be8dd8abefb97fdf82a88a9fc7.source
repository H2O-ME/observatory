# -*- coding: utf-8 -*-
"""HostSplit 客户端门槛单测（P0 修复回归，2026-10-26 生产事故复盘）。

生产形态：宿主 nginx（docker 网桥 172.x）反代到容器，转发 Host=127.0.0.1；
同时容器 0.0.0.0:8443 对公网开放（受控端直连）。安全边界必须是：

* 公网 IP + 伪造 ``Host: 127.0.0.1`` → 拒绝（0.3.7 之前的 fail-open）；
* 受信反代网段 + ``Host: 127.0.0.1`` → 放行（第一版修复误杀的生产主力流量）；
* 受信网段 + 外部 Host → 仍按域名表判定。
"""

from __future__ import annotations


import pytest
from starlette.types import Scope

from observatory.adapters.hostsplit import HostSplit


async def _run(middleware: HostSplit, scope: Scope) -> tuple[int, str]:
    """Drive the middleware with a raw ASGI scope; return (status, body)."""

    state: dict[str, object] = {}

    async def receive():  # pragma: no cover - not read for these requests
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            state["status"] = message["status"]
        elif message["type"] == "http.response.body":
            state["body"] = message.get("body", b"").decode("utf-8", "replace")

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware.app = inner  # type: ignore[assignment]
    await middleware(scope, receive, send)
    return int(state.get("status", 0)), str(state.get("body", ""))


def _scope(host: str, client_ip: str, path: str = "/api/v1/health") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "headers": [(b"host", host.encode())],
        "client": (client_ip, 50000),
        "server": ("127.0.0.1", 8443),
        "query_string": b"",
        "scheme": "https",
    }


def _middleware() -> HostSplit:
    async def app(scope, receive, send):  # pragma: no cover - replaced in _run
        raise AssertionError("unused")

    return HostSplit(
        app,  # type: ignore[arg-type]
        console_hosts=["control.example"],
        agent_hosts=["agent.example"],
    )


@pytest.mark.asyncio
async def test_public_client_with_forged_loopback_host_is_rejected():
    status, _ = await _run(_middleware(), _scope("127.0.0.1", "203.0.113.9"))
    assert status == 404


@pytest.mark.asyncio
async def test_loopback_client_with_loopback_host_passes():
    status, body = await _run(_middleware(), _scope("127.0.0.1", "127.0.0.1"))
    assert status == 200
    assert body == "ok"


@pytest.mark.asyncio
async def test_trusted_proxy_with_loopback_host_passes():
    """生产反代形态：宿主 nginx 从网桥 IP 进来，Host 就是 127.0.0.1。"""

    for proxy_ip in ("172.20.0.1", "172.18.0.5"):
        status, body = await _run(_middleware(), _scope("127.0.0.1", proxy_ip))
        assert status == 200, proxy_ip
        assert body == "ok"


@pytest.mark.asyncio
async def test_trusted_proxy_with_foreign_host_still_gated():
    """受信网段只豁免 loopback Host，不豁免未知域名。"""

    status, _ = await _run(_middleware(), _scope("evil.example", "172.20.0.1"))
    assert status == 404


@pytest.mark.asyncio
async def test_console_host_allowed_from_any_client():
    status, _ = await _run(_middleware(), _scope("control.example", "203.0.113.9", "/api/v1/devices"))
    assert status == 200


@pytest.mark.asyncio
async def test_custom_trusted_networks():
    async def app(scope, receive, send):  # pragma: no cover - replaced in _run
        raise AssertionError("unused")

    gate = HostSplit(
        app,  # type: ignore[arg-type]
        console_hosts=["control.example"],
        agent_hosts=["agent.example"],
        trusted_proxy_ips="10.1.0.0/16",
    )
    status_ok, _ = await _run(gate, _scope("127.0.0.1", "10.1.2.3"))
    status_bad, _ = await _run(gate, _scope("127.0.0.1", "172.20.0.1"))
    assert status_ok == 200
    assert status_bad == 404
