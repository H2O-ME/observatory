"""Optional off-host archive of captured frames.

Disabled by default (``archive_enabled``). When enabled, every stored frame is
also committed to a private GitHub repository through the Contents API, so a
stolen or lost server is not a lost history. The local copy still follows the
normal retention rules; this is a second copy, not a replacement.

The service is deliberately fire-and-forget: a failing archive must never delay or
fail a frame upload. Failures are logged and counted.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import httpx

from ..models import Frame

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
MAX_CONCURRENT_UPLOADS = 2


class ArchiveService:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
        self._tasks: set[asyncio.Task] = set()
        self.failures = 0
        self.uploads = 0

    # -- public API --------------------------------------------------------
    def schedule(
        self,
        *,
        enabled: bool,
        repo: str,
        token: str,
        branch: str,
        prefix: str,
        device_uid: str,
        frame: Frame,
        data: bytes,
    ) -> None:
        if not enabled or not repo or not token:
            return
        task = asyncio.create_task(
            self._upload(repo=repo, token=token, branch=branch, prefix=prefix, uid=device_uid, frame=frame, data=data)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def delete_device(self, device_uid: str, *, repo: str, token: str, branch: str, prefix: str) -> int:
        """Best-effort removal of a device's archive directory."""

        if not repo or not token:
            return 0
        removed = 0
        async with httpx.AsyncClient(timeout=30) as client:
            listing = await self._list(client, repo, token, branch, f"{prefix}/{device_uid}")
            for item in listing:
                if await self._delete(client, repo, token, branch, item["path"], item["sha"]):
                    removed += 1
        log.info("archive: removed %d file(s) for %s", removed, device_uid)
        return removed

    # -- internals ---------------------------------------------------------
    async def _upload(self, *, repo, token, branch, prefix, uid, frame: Frame, data: bytes) -> None:
        path = f"{prefix}/{uid}/{frame.path.split('/', 1)[-1]}"
        async with self._semaphore:
            try:
                async with httpx.AsyncClient(timeout=45) as client:
                    payload = {
                        "message": f"archive: {uid} {frame.ts_ms}",
                        "content": base64.b64encode(data).decode("ascii"),
                        "branch": branch,
                    }
                    response = await client.put(
                        f"{GITHUB_API}/repos/{repo}/contents/{path}",
                        headers=self._headers(token),
                        json=payload,
                    )
                if response.status_code in (200, 201):
                    self.uploads += 1
                else:
                    self.failures += 1
                    log.warning("archive upload failed %s: %s %s", path, response.status_code, response.text[:200])
            except Exception as exc:  # noqa: BLE001
                self.failures += 1
                log.warning("archive upload error %s: %s", path, exc)

    async def _list(self, client: httpx.AsyncClient, repo: str, token: str, branch: str, path: str) -> list[dict]:
        response = await client.get(
            f"{GITHUB_API}/repos/{repo}/contents/{path}",
            headers=self._headers(token),
            params={"ref": branch},
        )
        if response.status_code != 200:
            return []
        data = response.json()
        return [item for item in data if isinstance(item, dict) and item.get("type") == "file"]

    async def _delete(
        self, client: httpx.AsyncClient, repo: str, token: str, branch: str, path: str, sha: str
    ) -> bool:
        response = await client.request(
            "DELETE",
            f"{GITHUB_API}/repos/{repo}/contents/{path}",
            headers=self._headers(token),
            json={"message": f"purge: {path}", "sha": sha, "branch": branch},
        )
        return response.status_code in (200, 204)

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }


__all__ = ["ArchiveService"]