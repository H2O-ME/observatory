"""WS 下行泵：未确认堆积不得饿死新下行（0.3.1 F22 回归）。

真实事故（生产机 Docker 复现，2026-10-25）：DELETE 返回 200（ctrl_stop 已入队），
但泵把"未 ack 的旧信封"当成待发送工作，在热循环里重发同一批；ctrl_stop 被埋在
成千上万条重复之后，60s 内没有投递，idle 超时关掉会话（再次 DELETE 拿到 410）。

判据：本用例故意**不 ack** 任何下行（start/input 留在 outbox 里），只要求
ctrl_stop 出现在前 ``MAX_DOWNLINK`` 条下行内。热循环实现会在几十毫秒内灌满
这个窗口并失败。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

#: 修复后 stop 是第一条新 control；留足余量（含 pong/心跳级重投）。
MAX_DOWNLINK = 50

DEVICE_UID = "OBS-PUMP"
DEVICE_TOKEN = "pump-token"


def test_ctrl_stop_is_not_starved_by_unacked_backlog(tmp_dir) -> None:
    from tests.helpers import TEST_ADMIN_PASSWORD, running_app, seed_device

    def hello(msg_id: str) -> dict[str, object]:
        return {
            "v": 3,
            "id": msg_id,
            "ts_ms": 1_730_000_000_000,
            "t": "hello",
            "body": {
                "agent_version": "0.3.1",
                "protocol": 3,
                "boot_id": "boot-pump",
                "device_uid": DEVICE_UID,
                "last_ack_id": "",
                "device_time_ms": 1_730_000_000_000,
            },
        }

    with running_app(tmp_dir) as client:
        seed_device(client, uid=DEVICE_UID, token=DEVICE_TOKEN)
        login = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
        assert login.status_code == 200, login.text

        with client.websocket_connect(
            "/api/v1/agent/link", headers={"X-Device-Token": DEVICE_TOKEN}
        ) as agent_ws:
            agent_ws.send_json(hello("pump-hello"))
            assert agent_ws.receive_json()["t"] == "welcome"

            # 授权必须在链路上线后创建（没有活动链路时控制台拒绝授权）。
            granted = client.post(f"/api/v1/devices/{DEVICE_UID}/control", json={"confirm": True, "ttl_s": 120})
            assert granted.status_code == 200, granted.text
            token = granted.json()["data"]["token"]

            with client.websocket_connect(f"/api/v1/devices/{DEVICE_UID}/control?token={token}") as viewer:
                assert viewer.receive_json()["type"] == "ready"
                handled: set[str] = set()

                def next_control(ws, action: str) -> dict:
                    """读下一条 control，跳过 pong 与重复投递；**不 ack**（关键）。"""

                    while True:
                        message = ws.receive_json()
                        if message.get("t") == "pong" or message.get("id") in handled:
                            continue
                        assert message["t"] == "control", message
                        assert message["body"].get("action") == action, message
                        handled.add(message["id"])
                        return message

                # start 与 input 都**不 ack**：它们会一直留在 outbox 里。
                next_control(agent_ws, "start")
                viewer.send_json({"type": "input", "ev": {"kind": "move", "x": 1, "y": 2}})
                forwarded = next_control(agent_ws, "input")

                revoked = client.delete(f"/api/v1/devices/{DEVICE_UID}/control/{token}")
                assert revoked.status_code == 200, revoked.text

                seen: list[str] = []
                for _ in range(MAX_DOWNLINK):
                    message = agent_ws.receive_json()
                    body = message.get("body") or {}
                    seen.append(f"{message.get('t')}:{body.get('action') or '-'}")
                    if message.get("t") == "control" and body.get("action") == "stop":
                        assert message["id"] != forwarded["id"], "stop 必须是新信封"
                        break
                else:
                    pytest.fail(
                        f"ctrl_stop 未在前 {MAX_DOWNLINK} 条下行内投递"
                        f"（下行泵疑似在重发未确认信封）：{seen[:10]}"
                    )
