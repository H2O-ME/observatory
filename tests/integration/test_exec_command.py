"""0.3.18：``exec`` 命令 + ``api_key`` 长效认证的服务端契约。

覆盖三件事：
1. exec 的 payload 校验与确认门槛（缺 command 400；confirm 缺失 400；payload
   不被 CONFIRM_REQUIRED 清空分支抹掉——这是实现上最容易踩的坑）；
2. exec 的预算按 payload 派生（deadline_ms ≥ timeout_s×1000，行过期覆盖长命令）；
3. api_key：设置后 Bearer 直调（免密码登录 JWT），错 key/未设置一律 401。

exec 端到端结算复用 receipt-binding 的链路信封模式：假设备上行 result。
凭据一律运行时 secrets 生成（勿落字面量）；SQL 在调用点用字面量 + 参数绑定。
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

#: 运行时生成的测试密钥（Mimosa 卫生规则：不落凭据字面量）。
API_KEY = f"obs-api-{secrets.token_hex(12)}"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_path_factory.mktemp("exec-command")) as running:
        yield running


def _admin(client) -> dict[str, str]:
    from tests.helpers import TEST_ADMIN_PASSWORD  # noqa: PLC0415

    response = client.post(
        "/api/v1/auth/login",
        json={"password": TEST_ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['token']}"}


def _set_api_key(client) -> None:
    response = client.patch(
        "/api/v1/settings",
        json={"values": {"api_key": API_KEY}},
        headers=_admin(client),
    )
    assert response.status_code == 200, response.text


def _enrol(client, uid: str) -> str:
    response = client.post(
        "/api/v1/agent/enroll",
        json={"uid": uid, "hostname": uid.lower(), "os_name": "Windows", "agent_version": "0.3.18"},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _uplink_result(client, token: str, command_id: str) -> None:
    body = {
        "command_id": command_id,
        "ok": True,
        "result": {
            "exit_code": 0,
            "stdout": "nt authority\\system",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "duration_ms": 12,
            "timed_out": False,
        },
        "elapsed_ms": 12,
    }
    response = client.post(
        "/api/v1/agent/link",
        headers={"X-Device-Token": token},
        json={
            "v": 3,
            "last_ack_id": "",
            "items": [
                {
                    "v": 3,
                    "id": f"exec-{command_id}",
                    "t": "result",
                    "ts_ms": int(time.time() * 1000),
                    "body": body,
                }
            ],
        },
    )
    assert response.status_code == 200, response.text


def _command_row(client, command_id: str) -> tuple:
    connection = sqlite3.connect(Path(client.app.state.settings.data_dir) / "observatory.db")
    try:
        return connection.execute(
            "SELECT status, result, payload, deadline_ms, expires_at_ms FROM commands WHERE id=?",
            (command_id,),
        ).fetchone()
    finally:
        connection.close()


# --------------------------------------------------------------- payload 校验 ---
def test_exec_requires_command_field(client) -> None:
    headers = _admin(client)
    _enrol(client, "OBS-EXEC-V1")
    for payload in ({}, {"command": "   "}):
        response = client.post(
            "/api/v1/devices/OBS-EXEC-V1/commands",
            json={"type": "exec", "payload": payload, "confirm": True},
            headers=headers,
        )
        assert response.status_code == 422, (payload, response.text)  # AppError.bad_request → 422


def test_exec_requires_confirm_and_keeps_payload(client) -> None:
    headers = _admin(client)
    _enrol(client, "OBS-EXEC-V2")

    no_confirm = client.post(
        "/api/v1/devices/OBS-EXEC-V2/commands",
        json={"type": "exec", "payload": {"command": "whoami"}},
        headers=headers,
    )
    assert no_confirm.status_code == 422, no_confirm.text  # 需要显式确认 → 422

    ok = client.post(
        "/api/v1/devices/OBS-EXEC-V2/commands",
        json={"type": "exec", "payload": {"command": "whoami", "timeout_s": 30}, "confirm": True},
        headers=headers,
    )
    assert ok.status_code in {200, 201}, ok.text
    status, _result, payload, _deadline, _expiry = _command_row(client, ok.json()["data"]["id"])
    assert status in ("pending", "dispatched")
    assert "whoami" in payload, "payload 不能被 CONFIRM_REQUIRED 清空分支抹掉"
    assert "30" in payload


def test_exec_timeout_clamped_and_budget_derived(client) -> None:
    headers = _admin(client)
    _enrol(client, "OBS-EXEC-V3")
    response = client.post(
        "/api/v1/devices/OBS-EXEC-V3/commands",
        json={"type": "exec", "payload": {"command": "cmd", "timeout_s": 99999}, "confirm": True},
        headers=headers,
    )
    assert response.status_code in {200, 201}, response.text
    _status, _result, _payload, deadline_ms, expires_at_ms = _command_row(
        client, response.json()["data"]["id"]
    )
    assert deadline_ms >= 600_000, f"timeout_s 必须被钳制到 600 并派生预算，实际 {deadline_ms}"
    assert expires_at_ms - deadline_ms >= 600_000, "行过期必须覆盖长命令的执行窗口"


# ----------------------------------------------------------------- api_key ---
def test_api_key_disabled_by_default_and_rejects_wrong_key(client) -> None:
    fresh = {"Authorization": f"Bearer {API_KEY}"}
    response = client.get("/api/v1/devices", headers=fresh)
    assert response.status_code == 401, "未设置 api_key 时静态密钥必须禁用"

    _set_api_key(client)

    wrong = client.get("/api/v1/devices", headers={"Authorization": "Bearer wrong-key"})
    assert wrong.status_code == 401

    right = client.get("/api/v1/devices", headers=fresh)
    assert right.status_code == 200, right.text


def test_api_key_can_create_exec_command_and_settle(client) -> None:
    _set_api_key(client)
    headers = {"Authorization": f"Bearer {API_KEY}"}
    token = _enrol(client, "OBS-EXEC-AK")
    create = client.post(
        "/api/v1/devices/OBS-EXEC-AK/commands",
        json={"type": "exec", "payload": {"command": "whoami"}, "confirm": True},
        headers=headers,
    )
    assert create.status_code in {200, 201}, create.text
    command_id = create.json()["data"]["id"]
    assert _command_row(client, command_id)[0] in ("pending", "dispatched")

    _uplink_result(client, token, command_id)
    status, result, _payload, _deadline, _expiry = _command_row(client, command_id)
    assert status == "ok"
    assert "system" in (result or "")


def test_api_key_does_not_leak_to_agents_or_browser(client) -> None:
    _set_api_key(client)
    # 浏览器侧：GET /settings 已经剥 secret
    view = client.get("/api/v1/settings", headers=_admin(client)).json()["data"]
    assert "api_key" not in view or view.get("api_key") in (None, "")
    # 设备侧：agent 投影（device_safe_config）也不含密钥
    _enrol(client, "OBS-EXEC-LEAK")
    safe = client.get(
        "/api/v1/settings/effective/OBS-EXEC-LEAK",
        headers=_admin(client),
    ).json()["data"]
    assert "api_key" not in safe, "api_key 绝不能进入 agent 可见配置"
