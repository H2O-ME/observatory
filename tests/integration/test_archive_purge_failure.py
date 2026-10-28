"""S3 回归：远端归档删除失败不得吞成"成功清理"。

审计现场：远端 ConnectError 时 ``archive.purge_device`` 把失败降级为返回 0，
``ingest.delete_device_data`` 继续删帧行/归档账目，``device_purge`` 删设备行并
报告成功——结果 ``result_ok=True`` / ``archive_error=''`` / ``archive_rows_after=0``
/ ``device_exists_after=False``，远端副本却还在、账目没了。

这里用真 app + 假 GitHub（抛 ConnectError）走 ``purge_device_record`` 这条真实
调用链，断言失败被如实传播成 ``ok=False`` 并留下 ``purge_pending_at_ms`` 重试标记。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from observatory.media import archive, retention, storage

pytestmark = pytest.mark.integration

NOW = 1_700_000_000_000
DEVICE_UID = "OBS-S3-PURGE"
FRAME_ID = "s3-frame"
LOCAL_RELATIVE = f"{DEVICE_UID}/2024-01-02/12/a.webp"
DATA = b"fake-webp-frame-bytes-for-s3"


def _failing_factory() -> httpx.AsyncClient:
    """任何远端调用都按"连不上"处理（真实审计用的就是 ConnectError）。"""

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("mock connect error", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=5)


class _RemoteDeleteSpy:
    def __init__(self) -> None:
        self.calls = 0

    def factory(self) -> httpx.AsyncClient:
        self.calls += 1
        return _failing_factory()


def _archive_settings(media_root: Path) -> Any:
    from observatory.media.settings import defaults  # noqa: PLC0415

    return defaults().with_overrides(
        data_dir=media_root,
        frames_dir=media_root / "frames",
        thumbs_dir=media_root / "thumbs",
        archive_enabled=True,
        archive_repo="owner/repo",
        archive_token="test-token",
        archive_api_base="https://mock.api",
        archive_remote_delete=True,
    )


def _install_archive_settings(client: Any) -> None:
    """把归档开关写进 settings 表，让 media 层按"归档已就绪"解析。

    必须走 ``settings_store.patch``：``SettingsStore`` 有进程级缓存，直接
    INSERT 行不会让它失效；S3 的调用链（``device_purge ``→``store().load()``）
    读的正是这个缓存，否则会按"归档未启用"静默跳过远端删除。
    """

    from observatory.console.settings_store import get_store  # noqa: PLC0415
    from observatory.media import settings as media_settings  # noqa: PLC0415

    values = {
        "archive_enabled": True,
        "archive_repo": "owner/repo",
        "archive_token": "test-token",
        "archive_branch": "main",
        "archive_path_prefix": "frames",
    }

    async def write() -> None:
        async with client.app.state.db.sessionmaker() as session:
            await get_store().patch(session, values)
        media_settings.reset_cache()

    client.portal.call(write)


@pytest.fixture(autouse=True)
def _isolate_process_caches():
    """settings 的两层缓存都是进程级的；跨用例必须清掉，否则顺序决定结果。"""

    from observatory.console.settings_store import get_store  # noqa: PLC0415
    from observatory.media import settings as media_settings  # noqa: PLC0415

    get_store().invalidate()
    media_settings.reset_cache()
    yield
    get_store().invalidate()
    media_settings.reset_cache()


def _seed_rows(client: Any) -> tuple[int, int, int]:
    from observatory.core.clock import now_ms  # noqa: PLC0415
    from observatory.core.models import Device, Frame, FrameArchive  # noqa: PLC0415
    from observatory.core.crypto import device_token_hash  # noqa: PLC0415

    settings = client.app.state.settings
    digest = device_token_hash("s3-device-token", settings.secret_key)

    async def insert() -> tuple[int, int, int]:
        async with client.app.state.db.sessionmaker() as session:
            device = Device(
                uid=DEVICE_UID,
                name=DEVICE_UID,
                hostname="s3-host",
                os_name="Windows",
                os_version="11",
                arch="AMD64",
                agent_version="0.3.7",
                token_hash=digest,
                enrolled_at_ms=now_ms(),
                last_seen_ms=now_ms(),
                settings={},
                sysinfo={},
                note="s3",
            )
            session.add(device)
            await session.commit()
            await session.refresh(device)

            frame = Frame(
                device_id=int(device.id),
                ts_ms=NOW,
                session=0,
                kind="screen",
                frame_id=FRAME_ID,
                path=LOCAL_RELATIVE,
                thumb_path=None,
                width=1,
                height=1,
                size_bytes=len(DATA),
                sha256=hashlib.sha256(DATA).hexdigest(),
            )
            session.add(frame)
            row = FrameArchive(
                device_id=int(device.id),
                frame_id=FRAME_ID,
                day="2024-01-02",
                remote_path=archive.remote_path_for("frames", LOCAL_RELATIVE),
                sha256=hashlib.sha256(DATA).hexdigest(),
                size_bytes=len(DATA),
                status=archive.STATUS_UPLOADED,
                attempts=1,
                created_at_ms=NOW,
                error="",
            )
            session.add(row)
            await session.commit()
            await session.refresh(frame)
            await session.refresh(row)
            return int(device.id), int(frame.id), int(row.id)

    return client.portal.call(insert)


def _counts(client: Any) -> dict[str, int]:
    from sqlalchemy import func  # noqa: PLC0415

    from observatory.core.models import Device, Frame, FrameArchive  # noqa: PLC0415

    async def read() -> dict[str, int]:
        async with client.app.state.db.sessionmaker() as session:
            devices = await session.execute(select(Device).where(Device.uid == DEVICE_UID))
            device = devices.scalars().first()
            device_id = int(device.id) if device is not None else -1
            frames = int(
                (
                    await session.execute(
                        select(func.count()).select_from(Frame).where(Frame.device_id == device_id)
                    )
                ).scalar()
                or 0
            )
            archives = int(
                (
                    await session.execute(
                        select(func.count()).select_from(FrameArchive).where(FrameArchive.device_id == device_id)
                    )
                ).scalar()
                or 0
            )
            return {
                "devices": 1 if device is not None else 0,
                "frames": frames,
                "archives": archives,
                "purge_pending": 1 if device is not None and device.purge_pending_at_ms else 0,
            }

    return client.portal.call(read)


def _purge(client: Any) -> dict[str, Any]:
    from observatory.console.device_purge import purge_device_record  # noqa: PLC0415
    from observatory.core.models import Device  # noqa: PLC0415

    async def run() -> dict[str, Any]:
        async with client.app.state.db.sessionmaker() as session:
            device = (await session.execute(select(Device).where(Device.uid == DEVICE_UID))).scalars().first()
            assert device is not None
            return await purge_device_record(
                client.app, session, device, reason="pytest", purge_remote=True, ip="127.0.0.1"
            )

    return client.portal.call(run)


def test_remote_archive_delete_failure_keeps_accounts_and_reports_not_ok(tmp_dir: Path) -> None:
    from tests.helpers import running_app  # noqa: PLC0415

    media_root = tmp_dir / "media"
    settings = _archive_settings(media_root)
    local_absolute = storage.absolute(settings, archive.local_path_for("frames", LOCAL_RELATIVE))
    local_absolute.parent.mkdir(parents=True, exist_ok=True)
    local_absolute.write_bytes(DATA)

    spy = _RemoteDeleteSpy()
    static_config = None  # 记录被替换的服务，用后还原（同一 app 的 worker 不能泄漏）
    with running_app(tmp_dir / "app") as client:
        service = archive.get_service()
        static_config = (service, service._client_factory, service._api_base_override)
        service._client_factory = spy.factory
        service._api_base_override = "https://mock.api"
        try:
            _install_archive_settings(client)
            _seed_rows(client)
            before = _counts(client)
            assert before == {"devices": 1, "frames": 1, "archives": 1, "purge_pending": 0}

            result = _purge(client)

            # (1) 不得宣告清理成功
            assert result["ok"] is False, result
            assert result["error"], "失败必须带可读原因"
            assert "远端递归删除失败" in result["error"], result["error"]

            # (2) 远端删除确实被尝试过（不是"没调就当成功"）
            assert spy.calls >= 1

            # (3) 归档账目与帧行保留（否则远端副本再也定位不到）
            after = _counts(client)
            assert after["archives"] == 1, "远端删除失败时归档账目必须保留"
            assert after["frames"] == 1, "远端删除失败时帧行必须保留（供重试定位）"

            # (4) 设备行保留，并带上 purge_pending 重试标记
            assert after["devices"] == 1, "设备行必须保留到清理真正完成为止"
            assert after["purge_pending"] == 1, "必须复用 purge_pending_at_ms 让 housekeeping 重试"
        finally:
            original, factory, api_base = static_config
            original._client_factory = factory
            original._api_base_override = api_base


def test_remote_archive_delete_success_still_deletes_accounts(tmp_dir: Path) -> None:
    """成功路径不能被 S3 修复误伤：远端删成功后才删账目与设备行。"""

    from tests.helpers import running_app  # noqa: PLC0415

    media_root = tmp_dir / "media"
    settings = _archive_settings(media_root)
    local_absolute = storage.absolute(settings, archive.local_path_for("frames", LOCAL_RELATIVE))
    local_absolute.parent.mkdir(parents=True, exist_ok=True)
    local_absolute.write_bytes(DATA)

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and "/git/ref/heads/" in path:
            return httpx.Response(200, json={"object": {"sha": "commit-1"}}, request=request)
        if request.method == "GET" and path.endswith("/git/commits/commit-1"):
            return httpx.Response(200, json={"tree": {"sha": "tree-1"}}, request=request)
        if request.method == "GET" and path.endswith("/git/trees/tree-1"):
            return httpx.Response(
                200,
                json={
                    "tree": [{"path": f"frames/{DEVICE_UID}", "mode": "040000", "type": "tree", "sha": "d1"}],
                    "truncated": False,
                },
                request=request,
            )
        if request.method == "POST" and path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "tree-2"}, request=request)
        if request.method == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commit-2"}, request=request)
        if request.method == "PATCH" and "/git/refs/heads/" in path:
            return httpx.Response(200, json={"object": {"sha": "commit-2"}}, request=request)
        return httpx.Response(404, request=request)

    with running_app(tmp_dir / "app") as client:
        service = archive.get_service()
        originals = (service._client_factory, service._api_base_override)
        service._client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=5)
        service._api_base_override = "https://mock.api"
        try:
            _install_archive_settings(client)
            _seed_rows(client)
            result = _purge(client)
            assert result["ok"] is True, result
            assert int(result["media"]["remote_removed"]) == 1
            after = _counts(client)
            assert after == {"devices": 0, "frames": 0, "archives": 0, "purge_pending": 0}
        finally:
            service._client_factory, service._api_base_override = originals


def test_retention_gate_still_counts_archive_state() -> None:
    """S3 的失败语义不应改变保留门控：已上传/失败的门控取值保持原样。"""

    from observatory.media.settings import defaults  # noqa: PLC0415

    settings = defaults().with_overrides(
        archive_enabled=True,
        archive_repo="owner/repo",
        archive_token="t",
        archive_gate_retention=True,
    )
    assert retention.archive_gate_open(archive.STATUS_UPLOADED, NOW, settings, now_ms=NOW) is True
    assert retention.archive_gate_open(archive.STATUS_FAILED, NOW, settings, now_ms=NOW) is False
