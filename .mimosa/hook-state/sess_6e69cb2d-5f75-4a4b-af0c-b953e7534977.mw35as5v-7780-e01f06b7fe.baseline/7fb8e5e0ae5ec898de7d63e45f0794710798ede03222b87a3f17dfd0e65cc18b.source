"""真机复测回归：``uninstall_failed`` 事件必须立刻把卸载命令结算成 error。

2026-10-30 生产实测：设备回 ``{started:true}`` 之后 helper 秒退，随即上报
``uninstall_failed``。服务端只把事件写进日志，命令行一直是 ``running``，控制台
"正在执行远程卸载" 卡到 600s ACK 超时才变成 "未确认"——设备早就说了失败。

两条断言：
1. 失败事件是**这台设备自己**给出的证据，可以把它自己的未完成卸载结算成
   ``error``（``UNINSTALL_FAILED``），并带上原因；
2. S1 不放宽：A 的失败事件不能结算 B 的卸载。
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

    with running_app(tmp_path_factory.mktemp("uninstall-failed-event")) as running:
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
    _uplink(client, token, "hello", {"agent_version": "0.3.7", "protocol": 3, "boot_id": f"boot-{uid}"})
    return token


def _uplink(client, token: str, kind: str, body: dict) -> None:
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


def _start(client, token: str, command_id: str) -> None:
    _uplink(client, token, "result", {"command_id": command_id, "ok": True, "result": {"started": True}})


def _create_uninstall(client, headers, uid: str) -> str:
    response = client.post(f"/api/v1/devices/{uid}/uninstall", json={"confirm": True}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]["command_id"]


def test_failed_event_settles_own_uninstall_to_error(client) -> None:
    headers = _admin(client)
    token = _enrol(client, "OBS-FAIL-SELF")
    command_id = _create_uninstall(client, headers, "OBS-FAIL-SELF")
    _start(client, token, command_id)
    assert _sql(client, "SELECT status FROM commands WHERE id=?", (command_id,))[0][0] == "running"

    _uplink(
        client,
        token,
        "event",
        {"event": "uninstall_failed", "data": {"reason": "卸载 helper 在确认前退出（pid=4868）"}},
    )

    status, error, error_code = _sql(
        client, "SELECT status, error, error_code FROM commands WHERE id=?", (command_id,)
    )[0]
    assert status == "error", f"失败事件必须立刻结算成 error，实际 {status}"
    assert error_code == "UNINSTALL_FAILED"
    assert "helper" in (error or "")
    # 失败不能顺手把设备标成已卸载，也不能删记录。
    row = _sql(client, "SELECT COUNT(*), MAX(uninstalled_at_ms) FROM devices WHERE uid='OBS-FAIL-SELF'")[0]
    assert row[0] == 1 and row[1] is None


def test_failed_event_never_settles_another_devices_uninstall(client) -> None:
    headers = _admin(client)
    victim = _enrol(client, "OBS-FAIL-VICTIM")
    attacker = _enrol(client, "OBS-FAIL-ATTACKER")
    victim_command = _create_uninstall(client, headers, "OBS-FAIL-VICTIM")
    _start(client, victim, victim_command)

    _uplink(client, attacker, "event", {"event": "uninstall_failed", "data": {"reason": "伪造"}})
    # 攻击者自己没有被卸载命令；这条事件也不能给 B 结算。
    assert _sql(client, "SELECT status FROM commands WHERE id=?", (victim_command,))[0][0] == "running"
    assert _sql(client, "SELECT COUNT(*) FROM commands WHERE device_uid='OBS-FAIL-ATTACKER'")[0][0] == 0
