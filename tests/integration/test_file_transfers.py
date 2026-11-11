"""服务端暂存取回（v11）：发起 → 后台拉回 → 24h 暂存 → 下载/删除/过期。

关键契约（都是用户的原始要求）：
1. 点下载后取回由**服务端**执行，浏览器关掉/换页不影响；
2. 完成后从服务器下载，字节与设备源文件一致（sha256 可核对）；
3. 固定 24h 保留，到期/删除都先删行再删文件；
4. 设备中途失败必须如实标 failed 并清掉 ``.part``，不留半截文件；
5. 服务重启后残留的 running 行转 failed（内存任务已随进程消失）。

设备侧用假 session 直接返回 base64 分片，不经过真实链路；``ensure_reachable``
被替换成 no-op（它的"在线才可操作"语义由 files 测试覆盖，本文件测的是取回生命周期）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


@dataclass
class FakeResult:
    ok: bool = True
    result: dict[str, Any] | None = None
    error: str = ""
    error_code: str = ""
    status: str = "ok"
    latency_ms: int = 1


@dataclass
class FakeReadSession:
    """Returns ``files op=read`` slices from an in-memory payload."""

    payload: bytes
    fail_at: int | None = None
    block: bool = False
    calls: int = 0
    carrier: str = "http"
    last_seen_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    async def request(self, kind: str, body: dict[str, Any], *, timeout_s: float | None = None) -> FakeResult:
        assert kind == "files"
        assert body.get("op") == "read"
        self.calls += 1
        if self.block:
            await asyncio.sleep(3600)
        if self.fail_at is not None and self.calls >= self.fail_at:
            return FakeResult(ok=False, error="设备离线", error_code="GONE", status="error")
        offset = int(body.get("offset") or 0)
        length = int(body.get("length") or 0)
        chunk = self.payload[offset : offset + length]
        return FakeResult(
            result={
                "ok": True,
                "op": "read",
                "data": {
                    "op": "read",
                    "offset": offset,
                    "length": len(chunk),
                    "size": len(self.payload),
                    "eof": offset + len(chunk) >= len(self.payload),
                    "data": base64.b64encode(chunk).decode("ascii"),
                },
            },
        )


class FakeLinks:
    def __init__(self, session: FakeReadSession | None) -> None:
        self._session = session

    def get(self, device_id: int) -> FakeReadSession | None:
        return self._session


def _admin(client: Any) -> dict[str, str]:
    from tests.helpers import TEST_ADMIN_PASSWORD  # noqa: PLC0415

    response = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['token']}"}


def _enrol(client: Any, uid: str) -> None:
    response = client.post(
        "/api/v1/agent/enroll",
        json={"uid": uid, "hostname": uid.lower(), "os_name": "Windows", "agent_version": "0.3.26"},
    )
    assert response.status_code == 200, response.text


def _device_id(uid: str) -> int:
    from observatory.core.config import get_settings  # noqa: PLC0415

    connection = sqlite3.connect(str(get_settings().db_path))
    try:
        row = connection.execute("SELECT id FROM devices WHERE uid = ?", (uid,)).fetchone()
        assert row is not None, f"设备 {uid} 未入库"
        return int(row[0])
    finally:
        connection.close()


def _transfer_row(transfer_id: int) -> tuple[str, int, int, str]:
    from observatory.core.config import get_settings  # noqa: PLC0415

    connection = sqlite3.connect(str(get_settings().db_path))
    try:
        row = connection.execute(
            "SELECT status, received_bytes, expected_bytes, error FROM file_transfers WHERE id = ?",
            (transfer_id,),
        ).fetchone()
        assert row is not None, f"取回任务 {transfer_id} 不存在"
        return str(row[0]), int(row[1]), int(row[2]), str(row[3] or "")
    finally:
        connection.close()


def _wait_status(client: Any, headers: dict[str, str], transfer_id: int, wanted: set[str], timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get("/api/v1/file-transfers", headers=headers)
        assert response.status_code == 200, response.text
        items = response.json()["data"]["items"]
        last = next((item for item in items if item["id"] == transfer_id), {})
        if last.get("status") in wanted:
            return last
        time.sleep(0.02)
    raise AssertionError(f"等待状态 {wanted} 超时，最后一次 {last}")


def _spool_files() -> list[Path]:
    from observatory.core.config import get_settings  # noqa: PLC0415

    directory = get_settings().transfers_dir
    return sorted(path for path in directory.iterdir() if path.is_file())


@pytest.fixture()
def app_client(tmp_path_factory):
    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_path_factory.mktemp("file-transfers")) as client:
        yield client


def _patch_reachable(monkeypatch) -> None:
    from observatory.console import transfers  # noqa: PLC0415

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(transfers, "ensure_reachable", noop)


def test_transfer_completes_and_content_matches(app_client, monkeypatch) -> None:
    client = app_client
    _patch_reachable(monkeypatch)
    headers = _admin(client)
    uid = "OBS-TRANSFER-READY"
    _enrol(client, uid)
    payload = bytes(range(256)) * 10_000  # 2.5 MiB，强制多片
    session = FakeReadSession(payload=payload)
    monkeypatch.setattr(client.app.state, "links", FakeLinks(session))

    response = client.post(
        f"/api/v1/devices/{uid}/files/transfers",
        json={"path": "C:\\Users\\Public\\report.pdf", "size": len(payload)},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    created = response.json()["data"]
    assert created["status"] == "pending"
    assert created["name"] == "report.pdf"
    transfer_id = created["id"]

    ready = _wait_status(client, headers, transfer_id, {"ready", "failed"})
    assert ready["status"] == "ready", ready
    assert ready["received_bytes"] == len(payload)
    assert ready["expected_bytes"] == len(payload)
    assert ready["sha256"] == hashlib.sha256(payload).hexdigest()
    assert ready["expires_at_ms"] == ready["completed_at_ms"] + 24 * 60 * 60 * 1000
    assert ready["content_url"] == f"/api/v1/file-transfers/{transfer_id}/content"
    assert session.calls >= 3, "2.5MiB 必须分片读取"

    download = client.get(ready["content_url"], headers=headers)
    assert download.status_code == 200, download.text
    assert download.content == payload
    assert download.headers["x-sha256"] == hashlib.sha256(payload).hexdigest()
    assert "report.pdf" in download.headers["content-disposition"]
    assert _spool_files(), "ready 的暂存文件必须留在服务器上"

    # 任务行保留在列表里：设备快照不随设备改名/删除而丢
    assert ready["device_uid"] == uid


def test_device_failure_marks_failed_and_cleans_part(app_client, monkeypatch) -> None:
    client = app_client
    _patch_reachable(monkeypatch)
    headers = _admin(client)
    uid = "OBS-TRANSFER-FAIL"
    _enrol(client, uid)
    payload = b"x" * (2 * 1024 * 1024)
    session = FakeReadSession(payload=payload, fail_at=2)
    monkeypatch.setattr(client.app.state, "links", FakeLinks(session))

    response = client.post(
        f"/api/v1/devices/{uid}/files/transfers",
        json={"path": "C:\\big.bin", "size": len(payload)},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    transfer_id = response.json()["data"]["id"]

    failed = _wait_status(client, headers, transfer_id, {"ready", "failed"})
    assert failed["status"] == "failed"
    assert "设备离线" in failed["error"]
    assert failed["expires_at_ms"] is None, "失败的任务没有保留倒计时"
    assert _spool_files() == [], "失败必须删掉半截 .part"

    assert client.get(f"/api/v1/file-transfers/{transfer_id}/content", headers=headers).status_code == 422


def test_delete_cancels_running_and_removes_bytes(app_client, monkeypatch) -> None:
    client = app_client
    _patch_reachable(monkeypatch)
    headers = _admin(client)
    uid = "OBS-TRANSFER-DELETE"
    _enrol(client, uid)
    session = FakeReadSession(payload=b"y" * 4096, block=True)
    monkeypatch.setattr(client.app.state, "links", FakeLinks(session))

    response = client.post(
        f"/api/v1/devices/{uid}/files/transfers",
        json={"path": "C:\\slow.bin", "size": 4096},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    transfer_id = response.json()["data"]["id"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _transfer_row(transfer_id)[0] != "running":
        time.sleep(0.02)
    assert _transfer_row(transfer_id)[0] == "running"

    deleted = client.delete(f"/api/v1/file-transfers/{transfer_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["ok"] is True

    listing = client.get("/api/v1/file-transfers", headers=headers).json()["data"]["items"]
    assert all(item["id"] != transfer_id for item in listing)
    assert _spool_files() == []


def test_expiry_sweeps_row_before_file(app_client, monkeypatch) -> None:
    client = app_client
    _patch_reachable(monkeypatch)
    headers = _admin(client)
    uid = "OBS-TRANSFER-EXPIRE"
    _enrol(client, uid)
    payload = b"z" * 2048
    monkeypatch.setattr(client.app.state, "links", FakeLinks(FakeReadSession(payload=payload)))

    response = client.post(
        f"/api/v1/devices/{uid}/files/transfers",
        json={"path": "C:\\keep.bin", "size": len(payload)},
        headers=headers,
    )
    transfer_id = response.json()["data"]["id"]
    _wait_status(client, headers, transfer_id, {"ready", "failed"})
    assert _spool_files()

    async def expire() -> int:
        from sqlalchemy import text  # noqa: PLC0415

        from observatory.console.transfers import expire_transfers  # noqa: PLC0415

        async with client.app.state.db.sessionmaker() as session:
            await session.execute(
                text("UPDATE file_transfers SET expires_at_ms = 1 WHERE id = :id"),
                {"id": transfer_id},
            )
            await session.commit()
        return await expire_transfers(client.app)

    removed = client.portal.call(expire)
    assert removed >= 1
    listing = client.get("/api/v1/file-transfers", headers=headers).json()["data"]["items"]
    assert all(item["id"] != transfer_id for item in listing)
    assert _spool_files() == []


def test_restart_recovery_fails_interrupted_rows(app_client) -> None:
    client = app_client
    headers = _admin(client)
    uid = "OBS-TRANSFER-RESTART"
    _enrol(client, uid)

    async def seed_interrupted() -> int:
        from sqlalchemy import text  # noqa: PLC0415

        from observatory.console.transfers import recover_interrupted_transfers  # noqa: PLC0415
        from observatory.core.clock import now_ms  # noqa: PLC0415

        async with client.app.state.db.sessionmaker() as session:
            result = await session.execute(
                text(
                    "INSERT INTO file_transfers (device_uid, device_name, remote_path, filename, "
                    "expected_bytes, received_bytes, status, storage_name, actor, created_at_ms) "
                    "VALUES (:uid, :name, 'C:\\x.bin', 'x.bin', 100, 10, 'running', 'deadbeef.bin', "
                    "'console', :now) RETURNING id"
                ),
                {"uid": uid, "name": uid, "now": now_ms()},
            )
            transfer_id = int(result.scalar_one())
            await session.commit()
        part = client.app.state.settings.transfers_dir / "deadbeef.bin.part"
        part.write_bytes(b"partial")
        await recover_interrupted_transfers(client.app)
        assert not part.exists(), "中断任务的 .part 必须清掉"
        return transfer_id

    transfer_id = client.portal.call(seed_interrupted)
    listing = client.get("/api/v1/file-transfers", headers=headers).json()["data"]["items"]
    row = next(item for item in listing if item["id"] == transfer_id)
    assert row["status"] == "failed"
    assert "服务重启" in row["error"]
