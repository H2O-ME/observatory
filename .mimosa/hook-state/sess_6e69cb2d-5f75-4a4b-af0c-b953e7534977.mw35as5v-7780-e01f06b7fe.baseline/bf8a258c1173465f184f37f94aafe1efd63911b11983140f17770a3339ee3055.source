"""A14 归档闭环：上传→回读校验→才允许删本地；删设备递归删远端；失败可观测可重试。

用 httpx.MockTransport 模拟 GitHub Contents/Git Data API，不依赖真实仓库。
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from observatory.media import archive, retention, storage

pytestmark = pytest.mark.integration

NOW = 1_700_000_000_000
BASE_PATH = "frames"
DEVICE_UID = "OBS-ARCH"
REMOTE_BLOB_A = f"{BASE_PATH}/{DEVICE_UID}/2024-01-02/12/a.webp"
REMOTE_BLOB_B = f"{BASE_PATH}/{DEVICE_UID}/2024-01-03/09/b.webp"
REMOTE_OTHER = f"{BASE_PATH}/OBS-OTHER/2024-01-02/12/x.webp"


class FakeGitHub:
    """最小 GitHub 假实现：Contents PUT/GET + Git Trees 递归删除。"""

    def __init__(self, local_bytes: bytes, *, fail_put: bool = False, fail_tree: bool = False) -> None:
        self.local_bytes = local_bytes
        self.fail_put = fail_put
        self.fail_tree = fail_tree
        self.puts: list[str] = []
        self.readbacks = 0
        self.deleted_paths: list[str] = []
        self.tree_entries = [
            # Git Trees API 同时返回目录(tree)与文件(blob)；递归删除按最上层目录裁剪。
            {"path": f"{BASE_PATH}/{DEVICE_UID}", "mode": "040000", "type": "tree", "sha": "dir-root"},
            {"path": f"{BASE_PATH}/{DEVICE_UID}/2024-01-02", "mode": "040000", "type": "tree", "sha": "dir-a"},
            {"path": REMOTE_BLOB_A, "mode": "100644", "type": "blob", "sha": "a1"},
            {"path": f"{BASE_PATH}/{DEVICE_UID}/2024-01-03", "mode": "040000", "type": "tree", "sha": "dir-b"},
            {"path": REMOTE_BLOB_B, "mode": "100644", "type": "blob", "sha": "b1"},
            {"path": REMOTE_OTHER, "mode": "100644", "type": "blob", "sha": "x1"},
        ]

    def client_factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), timeout=5)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "PUT" and "/contents/" in path:
            self.puts.append(path)
            if self.fail_put:
                return httpx.Response(500, text="mock put failure", request=request)
            payload = json.loads(request.content)
            assert base64.b64decode(payload["content"]) == self.local_bytes
            return httpx.Response(201, json={"content": {"sha": "new"}}, request=request)
        if method == "GET" and "/contents/" in path:
            self.readbacks += 1
            return httpx.Response(
                200,
                json={"content": base64.b64encode(self.local_bytes).decode("ascii")},
                request=request,
            )
        if self.fail_tree:
            raise httpx.ConnectError("mock connect error", request=request)
        if method == "GET" and "/git/ref/heads/" in path:
            return httpx.Response(200, json={"object": {"sha": "commit-1"}}, request=request)
        if method == "GET" and path.endswith("/git/commits/commit-1"):
            return httpx.Response(200, json={"tree": {"sha": "tree-1"}}, request=request)
        if method == "GET" and path.endswith("/git/trees/tree-1"):
            return httpx.Response(200, json={"tree": self.tree_entries, "truncated": False}, request=request)
        if method == "POST" and path.endswith("/git/trees"):
            body = json.loads(request.content)
            self.deleted_paths = [str(entry["path"]) for entry in body.get("tree") or []]
            return httpx.Response(201, json={"sha": "tree-2"}, request=request)
        if method == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commit-2"}, request=request)
        if method == "PATCH" and "/git/refs/heads/" in path:
            return httpx.Response(200, json={"object": {"sha": "commit-2"}}, request=request)
        return httpx.Response(404, request=request)


def _archive_settings() -> Any:
    from observatory.media.settings import defaults

    return defaults().with_overrides(
        archive_enabled=True,
        archive_repo="owner/repo",
        archive_token="test-token",
        archive_api_base="https://mock.api",
        archive_verify_readback=True,
        archive_remote_delete=True,
        archive_gate_retention=True,
    )


def test_a14_archive_closure(tmp_dir: Path) -> None:
    from sqlalchemy import select

    from observatory.core.models import Device, FrameArchive
    from tests.helpers import running_app, seed_device

    media_root = tmp_dir / "media"
    settings = _archive_settings().with_overrides(
        data_dir=media_root,
        frames_dir=media_root / "frames",
        thumbs_dir=media_root / "thumbs",
    )
    frame_path = f"{DEVICE_UID}/2024-01-02/12/a.webp"
    remote = archive.remote_path_for(settings.archive_path_prefix, frame_path)
    local_relative = archive.local_path_for(settings.archive_path_prefix, remote)
    local_absolute = storage.absolute(settings, local_relative)
    data = b"fake-webp-frame-bytes"
    local_absolute.parent.mkdir(parents=True, exist_ok=True)
    local_absolute.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    mock = FakeGitHub(data)

    with running_app(tmp_dir) as client:
        device_id = seed_device(client, uid=DEVICE_UID, token="archive-device-token")

        async def insert_row(remote_path: str, frame_id: str) -> int:
            async with client.app.state.db.sessionmaker() as session:
                row = FrameArchive(
                    device_id=device_id,
                    frame_id=frame_id,
                    day="2024-01-02",
                    remote_path=remote_path,
                    sha256=digest,
                    size_bytes=len(data),
                    status=archive.STATUS_PENDING,
                    attempts=0,
                    created_at_ms=NOW,
                    error="",
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)
                return int(row.id)

        async def load_row(row_id: int) -> Any:
            async with client.app.state.db.sessionmaker() as session:
                return await session.get(FrameArchive, row_id)

        async def load_device(uid: str) -> Any:
            async with client.app.state.db.sessionmaker() as session:
                row = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()
                assert row is not None
                return row

        async def purge(uid: str) -> int:
            async with client.app.state.db.sessionmaker() as session:
                device = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()
                assert device is not None
                return await service.purge_device(session, device, settings=settings)

        row_id = client.portal.call(insert_row, remote, "frame-a")
        service = archive.ArchiveService(
            settings=settings,
            session_scope=client.app.state.db.session,
            client_factory=mock.client_factory,
            clock=lambda: NOW,
            api_base="https://mock.api",
        )

        async def process(job_id: int) -> None:
            await service._process(archive.ArchiveJob(job_id, settings))

        # 1) 成功上传 + 回读校验 → 已上传；本地文件此时仍在（保留门控决定何时删）
        client.portal.call(process, row_id)
        row = client.portal.call(load_row, row_id)
        assert row.status == archive.STATUS_UPLOADED, row.error
        assert mock.puts and mock.readbacks >= 1, "HTTP 200 不算成功，必须回读校验"
        assert local_absolute.exists()
        assert service.stats()["uploaded"] >= 1
        assert retention.archive_gate_open(archive.STATUS_UPLOADED, NOW, settings, now_ms=NOW) is True
        assert retention.archive_gate_open(archive.STATUS_FAILED, NOW, settings, now_ms=NOW) is False, (
            "归档失败未超宽限期时不允许删本地"
        )

        # 2) 上传失败 → 落库失败 + 退避可重试 + 统计可观测，且本地不删
        second_remote = f"{BASE_PATH}/{DEVICE_UID}/2024-01-03/09/b.webp"
        second_local = storage.absolute(
            settings, archive.local_path_for(settings.archive_path_prefix, second_remote)
        )
        second_local.parent.mkdir(parents=True, exist_ok=True)
        second_local.write_bytes(data)
        failing_row = client.portal.call(insert_row, second_remote, "frame-b")
        mock.fail_put = True
        client.portal.call(process, failing_row)
        failed = client.portal.call(load_row, failing_row)
        assert failed.status == archive.STATUS_FAILED
        assert int(failed.attempts) == 1
        assert "PUT 500" in str(failed.error or "")
        assert service.stats()["failed"] >= 1
        assert archive.retry_eligible(failed, settings, now_ms=NOW + 16_000) is True
        assert second_local.exists(), "上传失败不能删本地；应进入重试队列"
        assert retention.archive_gate_open(archive.STATUS_FAILED, NOW, settings, now_ms=NOW) is False

        # 3) 删除设备：Git Trees 递归删远端整棵设备子树，不碰别的设备
        mock.fail_put = False
        removed = client.portal.call(purge, DEVICE_UID)
        assert removed == 5, f"应统计设备子树内 5 个条目（目录+blob），实际 {removed}"
        assert mock.deleted_paths == [f"{BASE_PATH}/{DEVICE_UID}"], "应删最上层设备目录一次，不逐层扫"
        assert REMOTE_OTHER not in mock.deleted_paths, "不能误删其他设备"
        for row_id_to_check in (row_id, failing_row):
            row = client.portal.call(load_row, row_id_to_check)
            assert row.status == archive.STATUS_DELETED
        assert service.stats()["remote_deleted"] == 5

        # 4) 远端删除失败必须显式失败（S3）：旧实现返回 0 让上层当成"没东西可删"，
        #    继续删归档账目与设备记录并报告成功。现在抛 ArchiveError，账目保留可重试。
        other_id = seed_device(client, uid="OBS-ARCH2", token="archive-device-2")
        other_local = storage.absolute(
            settings, archive.local_path_for(settings.archive_path_prefix, REMOTE_BLOB_A)
        )
        other_local.parent.mkdir(parents=True, exist_ok=True)
        other_local.write_bytes(data)

        async def insert_other() -> int:
            async with client.app.state.db.sessionmaker() as session:
                row = FrameArchive(
                    device_id=other_id,
                    frame_id="frame-c",
                    day="2024-01-02",
                    remote_path=REMOTE_BLOB_A,
                    sha256=digest,
                    size_bytes=len(data),
                    status=archive.STATUS_UPLOADED,
                    attempts=0,
                    created_at_ms=NOW,
                    error="",
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)
                return int(row.id)

        other_row = client.portal.call(insert_other)
        mock.fail_tree = True
        with pytest.raises(archive.ArchiveError):
            client.portal.call(purge, "OBS-ARCH2")
        other = client.portal.call(load_row, other_row)
        assert other is not None, "失败时归档账目必须保留（否则无法定位远端副本）"
        assert other.status == archive.STATUS_UPLOADED, "删远端失败不改变'已上传'这一事实"
        assert "远端递归删除失败" in str(other.error or "")
        assert service._last_error, "失败必须可观测（_last_error / 备份页 stats）"

    # 5) 控制台备份页与接口存在（A14 最后一条）
    assert (Path(__file__).resolve().parents[2] / "webui/src/pages/backup.tsx").is_file()
    server = Path(__file__).resolve().parents[2] / "server/observatory/console/backup.py"
    text = server.read_text(encoding="utf-8")
    assert '@router.get("/backup")' in text
    assert "@router.get(\"/backup/entries\")" in text