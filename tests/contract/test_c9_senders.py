"""0.3.1 W1 契约：协议 v3 的每个下行类型都有真实发送方（C9）。

最大的回归是 0.3.0 的 update_offer 断链：agent 端接收逻辑完整、协议表也注册了，
服务端却没有任何代码发送它——自动更新闭环是断的。这里用真 app 验证下发。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import running_app, seed_agent_package, seed_device

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]


def _hello(msg_id: str, agent_version: str) -> dict:
    return {
        "v": 3,
        "id": msg_id,
        "t": "hello",
        "ts_ms": 1_730_000_000_000,
        "body": {
            "agent_version": agent_version,
            "protocol": 3,
            "boot_id": "offer-boot",
            "device_uid": "OBS-V3-TEST",
            "last_ack_id": "",
            "device_time_ms": 1_730_000_000_000,
        },
    }


def _heartbeat(msg_id: str, agent_version: str) -> dict:
    return {
        "v": 3,
        "id": msg_id,
        "t": "heartbeat",
        "ts_ms": 1_730_000_000_001,
        "body": {"agent_version": agent_version, "protocol": 3, "last_ack_id": ""},
    }


def test_c9_update_offer_flows_on_welcome_and_heartbeat(tmp_dir: Path) -> None:
    """新包 + auto_update + 版本更低 → welcome.update_offer 与 update_offer 信封。"""

    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-V3-TEST", token="v3-test-token", agent_version="0.3.0")
        # 测试设备的 settings 默认为空 → auto_update 走默认 True
        seed_agent_package(client, version="9.9.9")

        def link(items: list[dict], msg_id: str) -> dict:
            response = client.post(
                "/api/v1/agent/link",
                headers={"X-Device-Token": "v3-test-token"},
                json={"v": 3, "id": msg_id, "last_ack_id": "", "items": items},
            )
            assert response.status_code == 200, response.text
            return response.json()

        # welcome 带 update_offer
        body = link([_hello("c9-hello", "0.3.0")], "c9-req-1")
        welcome = next(item for item in body["items"] if item["t"] == "welcome")
        offer = welcome["body"].get("update_offer")
        assert offer, f"welcome 必须携带 update_offer: {json.dumps(welcome)[:300]}"
        assert offer["version"] == "9.9.9"
        assert len(offer.get("sha256") or "") == 64

        # 心跳回复带独立 update_offer 信封（WS 长连接的更新入口）。
        # 会话级节流 60s：welcome 刚查过的会话不会立刻再查，所以用一台新设备
        # （新会话、节流器归零）只发心跳来验证这条路径。
        seed_device(client, uid="OBS-V3-HB", token="v3-hb-token", agent_version="0.3.0")

        def link_hb(items: list[dict], msg_id: str) -> dict:
            response = client.post(
                "/api/v1/agent/link",
                headers={"X-Device-Token": "v3-hb-token"},
                json={"v": 3, "id": msg_id, "last_ack_id": "", "items": items},
            )
            assert response.status_code == 200, response.text
            return response.json()

        body = link_hb([_heartbeat("c9-hb", "0.3.0")], "c9-req-2")
        offers = [item for item in body["items"] if item["t"] == "update_offer"]
        assert offers, f"心跳回复必须带 update_offer: {json.dumps(body)[:300]}"
        assert offers[0]["body"]["version"] == "9.9.9"

        # 节流：同一会话第二次心跳（60s 内）不再重复查询
        body = link_hb([_heartbeat("c9-hb-2", "0.3.0")], "c9-req-3")
        assert not [item for item in body["items"] if item["t"] == "update_offer"], "60s 内应被节流"


def test_c9_no_offer_when_up_to_date_or_auto_update_off(tmp_dir: Path) -> None:
    """已是最新版本 → 无 offer；auto_update=False → 永不 offer（C8 有消费者）。"""

    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-V3-TEST", token="v3-test-token", agent_version="9.9.9")
        seed_agent_package(client, version="9.9.9")
        response = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": "v3-test-token"},
            json={"v": 3, "id": "c9-up-to-date", "last_ack_id": "", "items": [_hello("c9-hello-2", "9.9.9")]},
        )
        assert response.status_code == 200, response.text
        welcome = next(item for item in response.json()["items"] if item["t"] == "welcome")
        assert not welcome["body"].get("update_offer"), "同版本不应下发 offer"

        # 关掉 auto_update（per-device）
        async def disable() -> None:
            from observatory.core.models import Device

            async with client.app.state.db.sessionmaker() as session:
                device = (await session.execute(
                    __import__("sqlalchemy").select(Device).where(Device.uid == "OBS-V3-TEST")
                )).scalars().first()
                device.settings = {"auto_update": False}
                await session.commit()

        client.portal.call(disable)

        seed_agent_package(client, version="9.9.10")
        response = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": "v3-test-token"},
            json={"v": 3, "id": "c9-auto-off", "last_ack_id": "", "items": [_heartbeat("c9-hb-2", "9.9.9")]},
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert not [item for item in items if item["t"] == "update_offer"], "auto_update 关闭时不得下发"
        assert not [item for item in items if item["t"] == "welcome" and item["body"].get("update_offer")]


def test_c9_every_downlink_type_has_a_sender() -> None:
    """静态（C9）：envelope 下行类型表中每个类型在服务端代码里有发送点。"""

    from observatory.link.envelope import DOWNLINK_TYPES

    server = REPO_ROOT / "server" / "observatory"
    sources = {path: path.read_text(encoding="utf-8") for path in server.rglob("*.py")}
    senders = {
        "welcome": '"welcome"',
        "ping": '"ping"',
        "config": '"config"',
        "command": '"command"',
        "control": '"control"',
        "update_offer": '"update_offer"',
        "pong": '"pong"',
    }
    missing = []
    for kind in sorted(DOWNLINK_TYPES):
        needle = senders.get(kind, f'"{kind}"')
        if not any(needle in text for text in sources.values()):
            missing.append(kind)
    assert missing == [], f"下行类型 {missing} 没有任何发送点（C9 违规）"
