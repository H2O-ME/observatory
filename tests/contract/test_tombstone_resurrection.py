"""F9 根治：删除 → 重装 → 重新接入的完整设备生命周期。

事故（2026-10-25 生产）：真机删除后用 U 盘一键重装，enroll 被墓碑永久
拒绝，且管理员没有任何清除墓碑的正规入口。新语义：

* 一次性安装令牌 = 管理员重新接入该硬件的授权 —— enroll 持**未使用**
  令牌可击穿墓碑复活 uid，并在认领令牌的同一事务里烧掉墓碑行；
* 旧 device_token 永久失效（墓碑期 410，复活后 hash 未知 → 401）；
* 已用令牌不能复活任何 uid；无效令牌照旧在令牌校验层被拒。
"""

from pathlib import Path
from typing import Any
import sqlite3
import time

from tests.helpers import TEST_ADMIN_PASSWORD, running_app


def _age_out(tmp_dir: Path, uid: str) -> None:
    """enroll 会把 last_seen_ms 写成当下，删除端点会拒绝在线设备；
    用同步 sqlite3 把该行改老（WAL 允许与 app 的异步连接并发短写）。"""

    db_path = tmp_dir / "observatory.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE devices SET last_seen_ms = ? WHERE uid = ?",
            (int(time.time() * 1000) - 600_000, uid),
        )
        connection.commit()
    finally:
        connection.close()

_NOOP_ITEMS = [
    {
        "v": 3,
        "id": "resurrection-probe",
        "t": "event",
        "ts_ms": 1_790_000_000_000,
        "body": {"event": "noop", "data": {}},
    }
]


def _login(client: Any) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['token']}"}


def _mint(client: Any, headers: dict[str, str]) -> str:
    response = client.post("/api/v1/install-tokens", json={"ttl_s": 600}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]["token"]


def _enroll(client: Any, uid: str, install_token: str):
    return client.post(
        "/api/v1/agent/enroll",
        json={
            "uid": uid,
            "hostname": "resurrection-test",
            "os_name": "Windows",
            "os_version": "11",
            "arch": "AMD64",
            "agent_version": "0.3.1",
            "install_token": install_token,
        },
    )


def _link(client: Any, device_token: str):
    return client.post(
        "/api/v1/agent/link",
        headers={"X-Device-Token": device_token},
        json={"v": 3, "last_ack_id": "", "items": _NOOP_ITEMS},
    )


def test_delete_then_reinstall_with_fresh_token_reenrolls(tmp_dir: Path) -> None:
    with running_app(tmp_dir) as client:
        headers = _login(client)

        # 第一次安装
        first = _enroll(client, "OBS-REBORN-1", _mint(client, headers))
        assert first.status_code == 200, first.text
        old_device_token = first.json()["token"]

        # 管理员删除（写墓碑）→ 旧 device_token 立即终态 410
        _age_out(tmp_dir, "OBS-REBORN-1")
        gone = client.delete("/api/v1/devices/OBS-REBORN-1", headers=headers)
        assert gone.status_code == 200, gone.text
        assert _link(client, old_device_token).status_code == 410

        # 重装：新的一次性令牌 → 注册成功（击穿墓碑）
        second = _enroll(client, "OBS-REBORN-1", _mint(client, headers))
        assert second.status_code == 200, second.text
        new_device_token = second.json()["token"]
        assert new_device_token != old_device_token

        # 复活审计带 resurrected 标记
        audit_response = client.get("/api/v1/audit?limit=50", headers=headers)
        assert audit_response.status_code == 200, audit_response.text
        enrolls = [
            entry
            for entry in audit_response.json()["data"]["entries"]
            if entry.get("action") == "device.enroll" and entry.get("device_uid") == "OBS-REBORN-1"
        ]
        assert [entry["detail"].get("resurrected") for entry in enrolls] == [True, False]

        # 墓碑已烧掉：旧 token 不再 410 而是 401（hash 未知），新 token 正常上线
        assert _link(client, old_device_token).status_code == 401
        assert _link(client, new_device_token).status_code == 200


def test_used_token_cannot_resurrect_tombstoned_uid(tmp_dir: Path) -> None:
    with running_app(tmp_dir) as client:
        headers = _login(client)

        consumed = _mint(client, headers)
        assert _enroll(client, "OBS-TOMB-A", consumed).status_code == 200

        assert _enroll(client, "OBS-TOMB-B", _mint(client, headers)).status_code == 200
        _age_out(tmp_dir, "OBS-TOMB-B")
        deleted = client.delete("/api/v1/devices/OBS-TOMB-B", headers=headers)
        assert deleted.status_code == 200, deleted.text

        # B 拿 A 已用过的令牌复活 → 拒绝，且拒绝信息指向"曾被删除"
        response = _enroll(client, "OBS-TOMB-B", consumed)
        assert response.status_code == 401, response.text
        assert "删除" in response.text


def test_tombstoned_uid_with_invalid_token_still_rejected(tmp_dir: Path) -> None:
    with running_app(tmp_dir) as client:
        headers = _login(client)

        assert _enroll(client, "OBS-TOMB-C", _mint(client, headers)).status_code == 200
        _age_out(tmp_dir, "OBS-TOMB-C")
        assert client.delete("/api/v1/devices/OBS-TOMB-C", headers=headers).status_code == 200

        response = _enroll(client, "OBS-TOMB-C", "not-a-real-token")
        assert response.status_code == 401, response.text
