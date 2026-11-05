"""S5 回归：手动删除失败后必须真的会被重试，且重装/轮换必须撤销排队任务。

审计发现的现状：``purge_device_record`` 失败时会盖 ``purge_pending_at_ms``，
但 ``retry_pending_purges`` 的查询额外要求 ``uninstalled_at_ms IS NOT NULL``。
普通离线设备的手动删除没有这个标记，于是"将在后台重试"是一句空话，设备记录
永久保留。

v10 用 ``purge_reason`` / ``purge_remote`` 记录排队意图：重试只看意图，且重试
参数与最初请求一致。两条保护必须同时成立：

* 排队任务能被重试清掉（本文件前两条）；
* 设备重新注册/凭证轮换后旧任务失效，绝不误删新记录（后两条）。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_path_factory.mktemp("purge-retry")) as running:
        yield running


def _sql(client, statement: str, params: tuple = ()):
    db = Path(client.app.state.settings.data_dir) / "observatory.db"
    connection = sqlite3.connect(db)
    try:
        rows = connection.execute(statement, params).fetchall()
        connection.commit()
        return rows
    finally:
        connection.close()


def _admin(client) -> dict[str, str]:
    from tests.helpers import TEST_ADMIN_PASSWORD  # noqa: PLC0415

    response = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['token']}"}


def _enrol(client, uid: str) -> str:
    response = client.post(
        "/api/v1/agent/enroll",
        json={"uid": uid, "hostname": uid.lower(), "os_name": "Windows", "agent_version": "0.3.7"},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _age_out(client, uid: str, *, seconds: int = 600) -> None:
    """把 last_seen 推到过去，让设备进入离线态（在线设备拒绝删除记录）。"""

    _sql(
        client,
        "UPDATE devices SET last_seen_ms=? WHERE uid=?",
        (int(time.time() * 1000) - seconds * 1000, uid),
    )


def _retry_pass(client) -> int:
    maker = client.app.state.db.sessionmaker

    async def run() -> int:
        from observatory.console.device_purge import retry_pending_purges  # noqa: PLC0415

        async with maker() as session:
            return await retry_pending_purges(client.app, session)

    return client.portal.call(run)


def _break_media(monkeypatch) -> tuple[list[bool], object]:
    """让媒体清理失败，并记录每次调用传入的 ``purge_remote`` 参数。"""

    from observatory.media import ingest as media_ingest  # noqa: PLC0415

    seen: list[bool] = []
    real = media_ingest.delete_device_data

    async def failing(session, device, settings=None, *, purge_remote=True):
        seen.append(bool(purge_remote))
        raise RuntimeError("disk busy (injected)")

    monkeypatch.setattr(media_ingest, "delete_device_data", failing)
    return seen, real


def test_manual_delete_failure_is_retried_and_removes_record(client, monkeypatch) -> None:
    """手动删除失败 → 留下意图 → 下一拍真的被清理掉（旧实现永远 0）。"""

    headers = _admin(client)
    _enrol(client, "OBS-S5-MANUAL")
    _age_out(client, "OBS-S5-MANUAL")

    seen, real = _break_media(monkeypatch)
    failed = client.delete("/api/v1/devices/OBS-S5-MANUAL", headers=headers)
    assert failed.status_code == 500, failed.text
    assert seen == [True], "第一次删除应带上原始 purge_remote 参数"

    row = _sql(
        client,
        "SELECT purge_pending_at_ms, purge_reason, purge_remote FROM devices WHERE uid='OBS-S5-MANUAL'",
    )[0]
    assert row[0] is not None, "失败必须留下待重试标记"
    assert row[1] == "manual", f"排队意图必须记录，实际 {row[1]!r}"
    assert row[2] == 1

    # 媒体恢复后重试：必须真的把它清掉，而不是让记录永久留着
    from observatory.media import ingest as media_ingest  # noqa: PLC0415

    monkeypatch.setattr(media_ingest, "delete_device_data", real)
    assert _retry_pass(client) == 1
    assert _sql(client, "SELECT COUNT(*) FROM devices WHERE uid='OBS-S5-MANUAL'")[0][0] == 0


def test_manual_delete_keeps_purge_remote_false_on_retry(client, monkeypatch) -> None:
    """``purge_remote=False`` 的失败重试不能被悄悄升级成远端删除。"""

    headers = _admin(client)
    _enrol(client, "OBS-S5-NOREMOTE")
    _age_out(client, "OBS-S5-NOREMOTE")

    seen, real = _break_media(monkeypatch)
    failed = client.delete(
        "/api/v1/devices/OBS-S5-NOREMOTE", params={"purge_remote": "false"}, headers=headers
    )
    assert failed.status_code == 500, failed.text
    assert seen == [False]

    from observatory.media import ingest as media_ingest  # noqa: PLC0415

    monkeypatch.setattr(media_ingest, "delete_device_data", real)
    assert _retry_pass(client) == 1
    assert seen[-1] is False, "重试必须沿用当初的 purge_remote=False"
    assert _sql(client, "SELECT COUNT(*) FROM devices WHERE uid='OBS-S5-NOREMOTE'")[0][0] == 0


def test_reinstall_cancels_queued_manual_purge(client, monkeypatch) -> None:
    """排队中的手动删除，在设备重新注册后必须失效（不得误删新记录）。"""

    headers = _admin(client)
    token = _enrol(client, "OBS-S5-REVIVE")
    _age_out(client, "OBS-S5-REVIVE")

    _seen, real = _break_media(monkeypatch)
    failed = client.delete("/api/v1/devices/OBS-S5-REVIVE", headers=headers)
    assert failed.status_code == 500, failed.text
    assert _sql(client, "SELECT purge_reason FROM devices WHERE uid='OBS-S5-REVIVE'")[0][0] == "manual"

    # 普通轮换（设备没被标 uninstalled）：手动删除失败后重装走的就是这条路
    from observatory.media import ingest as media_ingest  # noqa: PLC0415

    monkeypatch.setattr(media_ingest, "delete_device_data", real)
    back = client.post(
        "/api/v1/agent/enroll",
        json={"uid": "OBS-S5-REVIVE", "hostname": "obs-s5-revive"},
        headers={"X-Device-Token": token},
    )
    assert back.status_code == 200, back.text

    marks = _sql(
        client,
        "SELECT purge_pending_at_ms, purge_reason, purge_remote FROM devices WHERE uid='OBS-S5-REVIVE'",
    )[0]
    assert marks == (None, None, None), f"重装必须撤销排队任务，实际 {marks}"
    assert _retry_pass(client) == 0
    assert _sql(client, "SELECT COUNT(*) FROM devices WHERE uid='OBS-S5-REVIVE'")[0][0] == 1


def test_rotation_during_media_step_keeps_new_record(client, monkeypatch) -> None:
    """清理进行中被重新注册：删除必须失败收场，活记录一条都不能少。

    "设备在排队后重装"由 enroll 清标记兜住；这里覆盖它兜不住的窄窗口——媒体清理
    跑到一半（SELECT 之后、DELETE 之前）凭证被轮换。删除前的见证比对 + 条件
    DELETE 必须让这次清理作废，而不是删掉刚注册回来的设备。
    """

    _enrol(client, "OBS-S5-RACE")
    device_id = _sql(client, "SELECT id FROM devices WHERE uid='OBS-S5-RACE'")[0][0]
    # 造出"任务已排队"的状态（等价于一次失败的删除留下的标记）
    _sql(
        client,
        "UPDATE devices SET purge_pending_at_ms=?, purge_reason='manual', purge_remote=1 WHERE id=?",
        (int(time.time() * 1000), device_id),
    )

    from observatory.media import ingest as media_ingest  # noqa: PLC0415

    db = Path(client.app.state.settings.data_dir) / "observatory.db"

    async def rotating(session, device, settings=None, *, purge_remote=True):
        # 媒体删除期间设备重新注册（token 轮换）：模拟真实的并发重装
        connection = sqlite3.connect(db)
        try:
            connection.execute(
                "UPDATE devices SET token_hash='rotated-by-reinstall', purge_reason=NULL "
                "WHERE id=?",
                (int(device.id),),
            )
            connection.commit()
        finally:
            connection.close()
        return {"frames": 0, "local_files": 0, "local_bytes": 0, "remote_removed": 0, "archive_error": ""}

    monkeypatch.setattr(media_ingest, "delete_device_data", rotating)
    assert _retry_pass(client) == 0, "清理期间轮换的旧任务不得算作成功"
    assert _sql(client, "SELECT COUNT(*) FROM devices WHERE uid='OBS-S5-RACE'")[0][0] == 1
