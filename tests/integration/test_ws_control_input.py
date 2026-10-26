"""实时控制输入洪峰不得堵死命令通道（2026-10-27 生产反馈回归）。

生产现象：开一会儿实时控制后，文件面板 `drives` 报
“受控端内部错误：下行队列已满，请稍后重试”，命令从未下发。

判据：400 条输入（>256 上限）灌进去之后，用控制通道自己的 ping/pong 做屏障
（pump_input 串行处理，pong 回来即代表 400 条都转发了），此时设备 outbox 的
未确认条数必须与洪峰前一致；随后创建的命令必须真的进了 outbox（修复前会在
入队时被拒，或在第 257 条输入上直接关掉控制 socket）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

DEVICE_UID = "OBS-INPUT"
DEVICE_TOKEN = "input-token"


def test_control_input_flood_does_not_saturate_the_command_outbox(tmp_dir) -> None:
    from tests.helpers import TEST_ADMIN_PASSWORD, running_app, seed_device

    hello = {
        "v": 3,
        "id": "input-hello",
        "ts_ms": 1_730_000_000_000,
        "t": "hello",
        "body": {
            "agent_version": "0.3.1",
            "protocol": 3,
            "boot_id": "boot-input",
            "device_uid": DEVICE_UID,
            "last_ack_id": "",
            "device_time_ms": 1_730_000_000_000,
        },
    }

    with running_app(tmp_dir) as client:
        device_id = seed_device(client, uid=DEVICE_UID, token=DEVICE_TOKEN)
        login = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
        assert login.status_code == 200, login.text

        with client.websocket_connect(
            "/api/v1/agent/link", headers={"X-Device-Token": DEVICE_TOKEN}
        ) as agent_ws:
            agent_ws.send_json(hello)
            assert agent_ws.receive_json()["t"] == "welcome"

            granted = client.post(f"/api/v1/devices/{DEVICE_UID}/control", json={"confirm": True, "ttl_s": 120})
            assert granted.status_code == 200, granted.text
            token = granted.json()["data"]["token"]

            with client.websocket_connect(f"/api/v1/devices/{DEVICE_UID}/control?token={token}") as viewer:
                assert viewer.receive_json()["type"] == "ready"
                session = client.app.state.links.get(device_id)  # type: ignore[attr-defined]
                assert session is not None
                # start 走可靠队列且用例不 ack，所以基线里本来就有 1 条。
                baseline = session.outbox.pending_count()
                assert baseline >= 1

                for index in range(400):
                    viewer.send_json(
                        {
                            "type": "input",
                            "ev": {"kind": "mouse", "action": "move", "x": index, "y": index, "norm": True},
                        }
                    )
                # 屏障：pump_input 串行处理，pong 回来时 400 条输入已全部转发。
                viewer.send_json({"type": "ping"})
                assert viewer.receive_json()["type"] == "pong"

                assert session.outbox.pending_count() == baseline, (
                    "输入事件挤进了可靠下行队列："
                    f"洪峰后未确认 {session.outbox.pending_count()} 条（原 {baseline} 条）"
                )

                created = client.post(
                    f"/api/v1/devices/{DEVICE_UID}/commands",
                    json={"type": "sysinfo", "payload": {}},
                )
                assert created.status_code == 200, created.text
                assert session.outbox.pending_count() == baseline + 1, (
                    "命令没有真正入队（可能被“下行队列已满”拒绝后又被静默吞掉）"
                )
