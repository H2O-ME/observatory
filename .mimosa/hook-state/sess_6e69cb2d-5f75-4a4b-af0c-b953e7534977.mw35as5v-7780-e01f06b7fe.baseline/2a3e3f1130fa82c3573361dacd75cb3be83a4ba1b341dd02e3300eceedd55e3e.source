"""S1/S2 回归：回执必须绑定认证设备；卸载只能被落盘 ACK 完成。

S1（P1）：回执此前只按客户端自报的 ``command_id`` 更新全局命令。设备 A 的令牌
可以把设备 B 的普通命令、卸载命令结算成 ok，甚至把 A 自己标成"已卸载"。
S2（P1）：第一次 ``{started:true}`` 让卸载进入 running，断线重连后重放同一回执
时 ``settle_started`` 返回 false 并落入普通结算，把没有 ACK 的卸载报成 ok。

两个入口都要覆盖：v3 链路信封（result / uninstall_ack）与
``POST /api/v1/agent/uninstall/ack``——它们共用 journal 与 confirm_uninstall，
但"共用"必须由测试证明，不能靠阅读。

文件共用一次 app 启动（模块级 fixture），每个用例使用互不相同的 UID。
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

    with running_app(tmp_path_factory.mktemp("receipt-binding")) as running:
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


def _uplink(client, token: str, kind: str, body: dict, *, envelope_id: str | None = None) -> None:
    response = client.post(
        "/api/v1/agent/link",
        headers={"X-Device-Token": token},
        json={
            "v": 3,
            "last_ack_id": "",
            "items": [
                {
                    "v": 3,
                    "id": envelope_id or f"bind-{kind}-{time.time_ns()}",
                    "t": kind,
                    "ts_ms": int(time.time() * 1000),
                    "body": body,
                }
            ],
        },
    )
    assert response.status_code == 200, response.text


def _command_status(client, command_id: str) -> str:
    return _sql(client, "SELECT status FROM commands WHERE id=?", (command_id,))[0][0]


def _uninstalled_at(client, uid: str):
    return _sql(client, "SELECT uninstalled_at_ms FROM devices WHERE uid=?", (uid,))[0][0]


def _open_uninstall(client, headers, uid: str) -> str:
    response = client.post(f"/api/v1/devices/{uid}/uninstall", json={"confirm": True}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]["command_id"]


def _sysinfo_command(client, headers, uid: str) -> str:
    response = client.post(f"/api/v1/devices/{uid}/commands", json={"type": "sysinfo"}, headers=headers)
    assert response.status_code in {200, 201}, response.text
    return response.json()["data"]["id"]


def test_device_cannot_settle_another_devices_commands(client) -> None:
    """S1：A 的令牌结算 B 的命令（普通 + 卸载 + started），B 的状态一律不变。"""

    headers = _admin(client)
    token_a = _enrol(client, "OBS-S1-A")
    # 只需要 B 存在：本用例验证 A 永远无法结算 B 的命令，不需要 B 的令牌。
    _enrol(client, "OBS-S1-B")

    regular_b = _sysinfo_command(client, headers, "OBS-S1-B")
    uninstall_b = _open_uninstall(client, headers, "OBS-S1-B")

    # A 试图结算 B 的普通命令
    _uplink(client, token_a, "result", {"command_id": regular_b, "ok": True, "result": {"fake": True}})
    # A 试图结算 B 的卸载命令（普通 ok 回执）
    _uplink(client, token_a, "result", {"command_id": uninstall_b, "ok": True, "result": {}})
    # A 试图用 started 把 B 的卸载推进 running
    _uplink(client, token_a, "result", {"command_id": uninstall_b, "ok": True, "result": {"started": True}})

    assert _command_status(client, regular_b) == "pending", "跨设备普通回执不得改变目标命令"
    assert _command_status(client, uninstall_b) == "pending", "跨设备卸载回执不得改变目标命令"
    assert _uninstalled_at(client, "OBS-S1-A") is None, "伪造者自己绝不能被标记已卸载"
    assert _uninstalled_at(client, "OBS-S1-B") is None

    # 跨设备尝试必须留痕（审计），否则"什么都没发生"等于"什么都没记录"
    audits = _sql(
        client,
        "SELECT COUNT(*) FROM audit WHERE action='command.foreign_result' AND device_uid='OBS-S1-A'",
    )[0][0]
    assert audits >= 3, f"每条跨设备回执都应写审计，实际 {audits}"


def test_cross_device_ack_refused_on_http_endpoint(client) -> None:
    """S1：HTTP ACK 用别人的 command_id → 403，且目标命令/双方设备状态都不变。"""

    headers = _admin(client)
    token_c = _enrol(client, "OBS-S1-C")
    token_d = _enrol(client, "OBS-S1-D")
    command_d = _open_uninstall(client, headers, "OBS-S1-D")

    forged = client.post(
        "/api/v1/agent/uninstall/ack",
        headers={"X-Device-Token": token_c},
        json={"command_id": command_d},
    )
    assert forged.status_code == 403, forged.text
    assert _command_status(client, command_d) == "pending"
    assert _uninstalled_at(client, "OBS-S1-C") is None
    assert _uninstalled_at(client, "OBS-S1-D") is None
    # 被拒绝的 ACK 不能顺手排一次清理
    assert _sql(client, "SELECT purge_pending_at_ms FROM devices WHERE uid='OBS-S1-C'")[0][0] is None

    # 合法 ACK 仍能完成：拒绝是"这条命令不属于你"，不是把设备拉黑
    legit = client.post(
        "/api/v1/agent/uninstall/ack",
        headers={"X-Device-Token": token_d},
        json={"command_id": command_d},
    )
    assert legit.status_code == 200, legit.text
    assert legit.json()["settled"] is True
    assert _command_status(client, command_d) == "ok"


def test_cross_device_ack_refused_on_legacy_uplink(client) -> None:
    """S1：0.3.7 的 uninstall_ack 信封入口同样受保护（两条入口一张网）。"""

    headers = _admin(client)
    token_e = _enrol(client, "OBS-S1-E")
    token_f = _enrol(client, "OBS-S1-F")
    command_f = _open_uninstall(client, headers, "OBS-S1-F")

    _uplink(client, token_e, "uninstall_ack", {"ok": True, "command_id": command_f})

    assert _command_status(client, command_f) == "pending", "链路信封入口也不得跨设备结算"
    assert _uninstalled_at(client, "OBS-S1-E") is None
    assert _uninstalled_at(client, "OBS-S1-F") is None

    # 真机（F）自己的 ACK 才能完成它
    _uplink(client, token_f, "uninstall_ack", {"ok": True, "command_id": command_f})
    assert _command_status(client, command_f) == "ok"
    assert _uninstalled_at(client, "OBS-S1-F") is not None


def test_replayed_started_keeps_running_until_real_ack(client) -> None:
    """S2：started → 重放 started → 仍是 running；ACK 之后才 ok。"""

    headers = _admin(client)
    token = _enrol(client, "OBS-S2-REPLAY")
    command_id = _open_uninstall(client, headers, "OBS-S2-REPLAY")

    # 第一次受理：pending → running
    _uplink(client, token, "result", {"command_id": command_id, "ok": True, "result": {"started": True}})
    assert _command_status(client, command_id) == "running"

    # 断线重连后重放同一信封（消息 id 会变，命令 id 不变）：必须幂等
    _uplink(client, token, "result", {"command_id": command_id, "ok": True, "result": {"started": True}})
    assert _command_status(client, command_id) == "running", "重放 started 不得改变状态，更不能变 ok"

    # 卸载的普通 ok 回执永远不能结算成功（没有落盘 ACK 就没有完成证据）
    _uplink(client, token, "result", {"command_id": command_id, "ok": True, "result": {"done": True}})
    assert _command_status(client, command_id) == "running", "普通回执不得把卸载结算成 ok"

    # 只有持久化 ACK 才是完成
    ack = client.post(
        "/api/v1/agent/uninstall/ack",
        headers={"X-Device-Token": token},
        json={"command_id": command_id},
    )
    assert ack.status_code == 200, ack.text
    assert ack.json()["settled"] is True
    assert _command_status(client, command_id) == "ok"
    assert _uninstalled_at(client, "OBS-S2-REPLAY") is not None
