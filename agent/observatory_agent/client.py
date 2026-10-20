"""HTTP client with automatic domain failover.

The fleet is reached through two hostnames so that a machine whose network blocks
one of them still works. Every request therefore goes through ``BaseRouter``:
on a transport-level failure the next base URL in the list is tried immediately and
the working one is remembered for the following requests.

Failover applies to *connections*, never to HTTP status codes: a 401 must not send
the agent chasing a second domain with a bad token.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from . import __version__

log = logging.getLogger(__name__)

DEVICE_TOKEN_HEADER = "X-Device-Token"
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 70.0
MAX_FAILOVER = 3


class TransportError(RuntimeError):
    """No base URL could be reached."""


def _verify_setting() -> bool | str:
    override = os.environ.get("OBS_TLS_INSECURE", "").strip().lower()
    if override in {"1", "true", "yes"}:
        return False
    ca_bundle = os.environ.get("OBS_CA_BUNDLE", "").strip()
    if ca_bundle:
        return ca_bundle
    return True


class BaseRouter:
    def __init__(self, urls: list[str]) -> None:
        cleaned = [url.rstrip("/") for url in urls if url.strip()]
        self._urls = cleaned or ["https://control.tianhw.top"]
        self._index = 0

    @property
    def urls(self) -> list[str]:
        return list(self._urls)

    @property
    def current(self) -> str:
        return self._urls[self._index % len(self._urls)]

    def set_urls(self, urls: list[str]) -> None:
        cleaned = [url.rstrip("/") for url in urls if url.strip()]
        if cleaned:
            self._urls = cleaned
            self._index = 0

    def rotate(self) -> str:
        self._index = (self._index + 1) % len(self._urls)
        return self.current

    def candidates(self) -> list[str]:
        return [self._urls[(self._index + offset) % len(self._urls)] for offset in range(len(self._urls))]


class AgentClient:
    def __init__(self, router: BaseRouter, token: str = "") -> None:
        self.router = router
        self.token = token
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            verify=_verify_setting(),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"User-Agent": f"ObservatoryAgent/{__version__}"},
        )

    # -- plumbing ----------------------------------------------------------
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers[DEVICE_TOKEN_HEADER] = self.token
        if extra:
            headers.update(extra)
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        data: bytes | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        retries: int = MAX_FAILOVER,
    ) -> httpx.Response:
        """Perform a request, rotating base URLs on connection failures."""

        last_error: Exception | None = None
        attempts = max(1, min(retries, len(self.router.urls)))
        for _ in range(attempts):
            url = f"{self.router.current}{path}"
            try:
                response = await self._http.request(
                    method,
                    url,
                    json=json,
                    content=data,
                    params=params,
                    headers=self._headers(headers),
                    timeout=timeout or READ_TIMEOUT_S,
                )
                return response
            except (httpx.TransportError, httpx.TimeoutException, OSError) as exc:
                last_error = exc
                log.warning("%s %s failed via %s: %s", method, path, self.router.current, exc)
                self.router.rotate()
        raise TransportError(f"{method} {path} 全部域名不可达: {last_error}")

    async def close(self) -> None:
        await self._http.aclose()

    # -- endpoints ---------------------------------------------------------
    async def enroll(
        self,
        *,
        code: str,
        uid: str,
        hostname: str,
        os_name: str,
        os_version: str,
        arch: str,
        agent_version: str,
        sysinfo: dict,
        sessions: list[dict],
    ) -> dict:
        response = await self.request(
            "POST",
            "/api/v1/agent/enroll",
            json={
                "code": code,
                "uid": uid,
                "hostname": hostname,
                "os_name": os_name,
                "os_version": os_version,
                "arch": arch,
                "agent_version": agent_version,
                "sysinfo": sysinfo,
                "sessions": sessions,
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise TransportError(f"注册失败 HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    async def heartbeat(self, payload: dict) -> dict:
        response = await self.request("POST", "/api/v1/agent/heartbeat", json=payload, timeout=30)
        if response.status_code == 401:
            raise PermissionError("设备令牌失效")
        if response.status_code != 200:
            raise TransportError(f"心跳失败 HTTP {response.status_code}")
        return response.json()

    async def poll_commands(self, wait: int) -> dict:
        response = await self.request(
            "GET", "/api/v1/agent/commands", params={"wait": wait}, timeout=wait + 25
        )
        if response.status_code == 401:
            raise PermissionError("设备令牌失效")
        if response.status_code != 200:
            raise TransportError(f"轮询失败 HTTP {response.status_code}")
        return response.json()

    async def send_result(self, command_id: str, ok: bool, result: dict | None, error: str | None) -> bool:
        try:
            response = await self.request(
                "POST",
                f"/api/v1/agent/commands/{command_id}/result",
                json={"ok": ok, "result": result, "error": error},
                timeout=30,
            )
            return response.status_code == 200
        except TransportError:
            return False

    async def upload_frame(self, data: bytes, *, ts_ms: int, session_id: int, kind: str = "screen") -> bool:
        try:
            response = await self.request(
                "POST",
                "/api/v1/agent/frames",
                data=data,
                params={"ts_ms": ts_ms, "session_id": session_id, "kind": kind},
                headers={"Content-Type": "image/webp"},
                timeout=45,
            )
            return response.status_code == 200
        except TransportError:
            return False

    async def get_state(self) -> dict:
        response = await self.request("GET", "/api/v1/agent/state", timeout=20)
        if response.status_code != 200:
            raise TransportError(f"状态查询失败 HTTP {response.status_code}")
        return response.json()

    async def download_package(self, version: str, destination: Path, expected_sha256: str) -> str:
        """Download and verify an agent package. Returns the sha256 actually seen."""

        response = await self.request(
            "GET", f"/api/v1/public/agent/{version}/package", timeout=300, retries=MAX_FAILOVER
        )
        if response.status_code != 200:
            raise TransportError(f"下载安装包失败 HTTP {response.status_code}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(".part")
        digest = hashlib.sha256()
        with open(tmp, "wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)
                digest.update(chunk)
        actual = digest.hexdigest()
        if expected_sha256 and actual != expected_sha256:
            tmp.unlink(missing_ok=True)
            raise TransportError(f"安装包校验失败: {actual[:12]} != {expected_sha256[:12]}")
        os.replace(tmp, destination)
        return actual

    async def notify_uninstalled(self) -> None:
        try:
            await self.request("POST", "/api/v1/agent/uninstall/ack", json={}, timeout=15)
        except TransportError:
            pass

    def ws_url(self) -> str:
        base = self.router.current
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :] + "/api/v1/agent/ws"
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :] + "/api/v1/agent/ws"
        return base + "/api/v1/agent/ws"


async def wait(seconds: float) -> None:
    await asyncio.sleep(max(0.0, seconds))


_ = asyncio

__all__ = ["AgentClient", "BaseRouter", "DEVICE_TOKEN_HEADER", "MAX_FAILOVER", "TransportError"]