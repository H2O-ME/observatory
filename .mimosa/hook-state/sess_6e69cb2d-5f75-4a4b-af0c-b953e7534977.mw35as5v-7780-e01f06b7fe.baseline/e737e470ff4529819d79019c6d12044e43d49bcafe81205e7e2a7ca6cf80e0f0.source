"""P2：卸载状态机 —— 受理≠完成、ACK 才结算、超时=未确认、单设备唯一待确认。

跑得很快：全部走 HTTP 接口与 journal，不依赖长轮询也不依赖 Windows。
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

    with running_app(tmp_path_factory.mktemp("uninstall-ack")) as running:
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
    token = response.json()["token"]
    # 卸载命令需要活动链路（capabilities.uninstall 由 presence 判定），
    # hello 会即时返回 welcome，不触发长轮询挂起。
    _uplink(client, token, "hello", {"agent_version": "0.3.7", "protocol": 3, "boot_id": f"boot-{uid}"})
    return token


def _uplink(client, token: str, kind: str, body: dict) -> None:
    """一条上行信封（带 event/hello 之类，避免触发长轮询挂起）。"""

    response = client.post(
        "/api/v1/agent/link",
        headers={"X-Device-Token": token},
        json={
            "v": 3,
            "last_ack_id": "",
            "items": [
                {
                    "v": 3,
                    "id": f"p2-{kind}-{time.time_ns()}",
                    "t": kind,
                    "ts_ms": int(time.time() * 1000),
                    "body": body,
                }
            ],
        },
    )
    assert response.status_code == 200, response.text


def _started(client, token: str, command_id: str) -> None:
    _uplink(client, token, "result", {"command_id": command_id, "ok": True, "result": {"started": True}})


def _uninstall(client, headers, uid: str) -> dict:
    response = client.post(f"/api/v1/devices/{uid}/uninstall", json={"confirm": True}, headers=headers)
    return {"status": response.status_code, "body": response.json()}


def test_started_marks_running_not_ok(client) -> None:
    """卸载处理器回 {started:true} 只是受理：命令必须是 running，设备不得标已卸载。"""

    headers = _admin(client)
    token = _enrol(client, "OBS-P2-RUN")
    dispatched = _uninstall(client, headers, "OBS-P2-RUN")
    assert dispatched["status"] == 200, dispatched
    command_id = dispatched["body"]["data"]["command_id"]

    _started(client, token, command_id)

    row = _sql(client, "SELECT status FROM commands WHERE id=?", (command_id,))[0]
    assert row[0] == "running", f"受理后必须 running，实际 {row[0]}"
    assert _sql(client, "SELECT uninstalled_at_ms FROM devices WHERE uid='OBS-P2-RUN'")[0][0] is None


def _purge_pass(client) -> int:
    """跑一次清理通道（生产由 housekeeping 每 20s 触发）。"""

    maker = client.app.state.db.sessionmaker

    async def run() -> int:
        from observatory.console.device_purge import retry_pending_purges  # noqa: PLC0415

        async with maker() as session:
            return await retry_pending_purges(client.app, session)

    return client.portal.call(run)


def test_ack_endpoint_settles_ok_then_purge_removes_record(client) -> None:
    """ACK 结算 ok 并排队清理；清理后设备消失、命令历史与审计保留。

    这是"卸载后从设备墙消失，但仍可查历史"的核心断言。
    """

    headers = _admin(client)
    token = _enrol(client, "OBS-P2-ACK")
    command_id = _uninstall(client, headers, "OBS-P2-ACK")["body"]["data"]["command_id"]
    _started(client, token, command_id)

    ack = client.post(
        "/api/v1/agent/uninstall/ack",
        headers={"X-Device-Token": token},
        json={"command_id": command_id, "stage": "prepared", "helper_pid": 4321},
    )
    assert ack.status_code == 200, ack.text
    body = ack.json()
    assert body["settled"] is True and body["uninstalled"] is True
    assert body["purge_pending"] is True, body

    # 命令已结算，设备已标记卸载，但记录还在（清理是下一拍）
    row = _sql(client, "SELECT status FROM commands WHERE id=?", (command_id,))[0]
    assert row[0] == "ok"
    assert _sql(client, "SELECT COUNT(*) FROM devices WHERE uid='OBS-P2-ACK'")[0][0] == 1
    assert _sql(client, "SELECT uninstalled_at_ms FROM devices WHERE uid='OBS-P2-ACK'")[0][0] is not None

    # 清理一拍后：记录消失
    assert _purge_pass(client) >= 1
    assert _sql(client, "SELECT COUNT(*) FROM devices WHERE uid='OBS-P2-ACK'")[0][0] == 0

    # 命令历史保留（v8：device_id 置 NULL，device_uid 仍在）
    history_row = _sql(client, "SELECT status, device_id, device_uid FROM commands WHERE id=?", (command_id,))[0]
    assert history_row[0] == "ok" and history_row[1] is None and history_row[2] == "OBS-P2-ACK"

    history = client.get("/api/v1/devices/history/records", params={"uid": "OBS-P2-ACK"}, headers=headers)
    assert history.status_code == 200, history.text
    ids = [item["id"] for item in history.json()["data"]["commands"]]
    assert command_id in ids

    audits = _sql(
        client,
        "SELECT COUNT(*) FROM audit WHERE action='device.uninstall' AND device_uid='OBS-P2-ACK'",
    )[0][0]
    assert audits >= 1


def test_repeat_ack_is_idempotent(client) -> None:
    """重复 ACK 仍 200 且不重复结算；清理前重试仍能认证（幂等窗口）。"""

    headers = _admin(client)
    token = _enrol(client, "OBS-P2-IDEM")
    command_id = _uninstall(client, headers, "OBS-P2-IDEM")["body"]["data"]["command_id"]
    _started(client, token, command_id)

    first = client.post(
        "/api/v1/agent/uninstall/ack",
        headers={"X-Device-Token": token},
        json={"command_id": command_id},
    )
    assert first.status_code == 200, first.text
    assert first.json()["settled"] is True

    # 响应丢失后的重试：必须在清理拍之前仍然 200，否则"幂等"形同虚设
    again = client.post(
        "/api/v1/agent/uninstall/ack",
        headers={"X-Device-Token": token},
        json={"command_id": command_id},
    )
    assert again.status_code == 200, again.text
    assert again.json()["settled"] is False, "重复 ACK 不应再次结算"
    assert again.json()["uninstalled"] is True, "但必须仍然回答'已卸载'"


def test_reinstall_cancels_queued_purge(client) -> None:
    """已排队清理的设备若重装，不能被清理通道误删（守卫）。"""

    headers = _admin(client)
    token = _enrol(client, "OBS-P2-REVIVE")
    command_id = _uninstall(client, headers, "OBS-P2-REVIVE")["body"]["data"]["command_id"]
    _started(client, token, command_id)
    client.post(
        "/api/v1/agent/uninstall/ack",
        headers={"X-Device-Token": token},
        json={"command_id": command_id},
    )
    # 重装（带旧令牌证明身份的重新准入）
    back = client.post(
        "/api/v1/agent/enroll",
        json={"uid": "OBS-P2-REVIVE", "hostname": "obs-p2-revive", "prev_token": token},
    )
    assert back.status_code == 200, back.text

    _purge_pass(client)
    assert _sql(client, "SELECT COUNT(*) FROM devices WHERE uid='OBS-P2-REVIVE'")[0][0] == 1, (
        "重装后不得被排队的清理删掉"
    )


def test_ack_timeout_becomes_unconfirmed_and_keeps_record(client) -> None:
    """ACK 超时 = 结果未知：命令 unconfirmed（不是 failed），设备记录保留。"""

    headers = _admin(client)
    token = _enrol(client, "OBS-P2-TIMEOUT")
    command_id = _uninstall(client, headers, "OBS-P2-TIMEOUT")["body"]["data"]["command_id"]
    _started(client, token, command_id)

    # 把 ACK 截止时间推到过去，模拟长时间无回执
    _sql(client, "UPDATE commands SET expires_at_ms=? WHERE id=?", (int(time.time() * 1000) - 1000, command_id))

    from observatory.core.clock import now_ms  # noqa: PLC0415

    expired = client.portal.call(lambda: client.app.state.journal.expire_uninstall_acks(now=now_ms()))
    assert expired == 1

    row = _sql(client, "SELECT status, error_code FROM commands WHERE id=?", (command_id,))[0]
    assert row[0] == "unconfirmed", row
    assert row[1] == "ACK_TIMEOUT"
    assert _sql(client, "SELECT COUNT(*) FROM devices WHERE uid='OBS-P2-TIMEOUT'")[0][0] == 1, (
        "沉默不能推断成卸载成功，记录必须保留"
    )

    # 迟到但真实的 ACK 可以升级 unconfirmed → ok（新证据），随后才删记录
    late = client.post(
        "/api/v1/agent/uninstall/ack",
        headers={"X-Device-Token": token},
        json={"command_id": command_id},
    )
    assert late.status_code == 200, late.text
    assert _sql(client, "SELECT status FROM commands WHERE id=?", (command_id,))[0][0] == "ok"
    # P3：清理前记录仍在（清理是排队的一拍），跑一次清理后才消失
    _purge_pass(client)
    assert _sql(client, "SELECT COUNT(*) FROM devices WHERE uid='OBS-P2-TIMEOUT'")[0][0] == 0


def test_second_uninstall_while_open_is_409(client) -> None:
    """单设备唯一未终结卸载：第二次请求必须 409，不能叠出两条命令。"""

    headers = _admin(client)
    token = _enrol(client, "OBS-P2-DUP")
    first = _uninstall(client, headers, "OBS-P2-DUP")
    assert first["status"] == 200
    _started(client, token, first["body"]["data"]["command_id"])

    second = _uninstall(client, headers, "OBS-P2-DUP")
    assert second["status"] == 409, second
    assert "uninstall.in_progress" in str(second["body"])

    open_rows = _sql(
        client,
        "SELECT COUNT(*) FROM commands WHERE device_id=(SELECT id FROM devices WHERE uid='OBS-P2-DUP') "
        "AND type='uninstall' AND status IN ('pending','dispatched','running')",
    )[0][0]
    assert open_rows == 1


def test_legacy_ack_without_command_id_settles_single_open_task(client) -> None:
    """0.3.7 兼容：无 command_id 的 outbox ack 结算唯一未终结卸载任务。"""

    headers = _admin(client)
    token = _enrol(client, "OBS-P2-LEGACY")
    command_id = _uninstall(client, headers, "OBS-P2-LEGACY")["body"]["data"]["command_id"]
    _started(client, token, command_id)

    _uplink(client, token, "uninstall_ack", {"ok": True})

    assert _sql(client, "SELECT status FROM commands WHERE id=?", (command_id,))[0][0] == "ok"
    _purge_pass(client)
    assert _sql(client, "SELECT COUNT(*) FROM devices WHERE uid='OBS-P2-LEGACY'")[0][0] == 0


def test_ack_requires_device_token(client) -> None:
    """ACK 端点只认设备凭证：UID 或裸请求都不能确认卸载。"""

    no_token = client.post("/api/v1/agent/uninstall/ack", json={"command_id": "whatever"})
    assert no_token.status_code == 401
    bad_token = client.post(
        "/api/v1/agent/uninstall/ack",
        headers={"X-Device-Token": "not-a-token"},
        json={"command_id": "whatever"},
    )
    assert bad_token.status_code == 401
