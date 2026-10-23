"""集成：真起新服务端（app 装配 + 真 DB 迁移 + media worker + legacy 路由）。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from tests.helpers import legacy_token, running_app

pytestmark = pytest.mark.integration


def test_server_boots_and_serves_health(tmp_dir: Path) -> None:
    with running_app(tmp_dir) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200, health.text
        body = health.json()
        assert body["ok"] is True
        assert body["version"].startswith("0.3.")
        assert isinstance(body["server_time_ms"], int) and body["server_time_ms"] > 0

        version = client.get("/api/v1/version")
        assert version.status_code == 200
        assert version.json()["version"].startswith("0.3.")

        # 未登录访问控制台接口 → 结构化 401（不是 500/404）
        devices = client.get("/api/v1/devices")
        assert devices.status_code == 401
        assert devices.json()["code"] == "AUTH"

    # A11：HostSplit fail-closed —— 未知 Host 拿不到控制台（统一 404）
    with running_app(tmp_dir) as client:
        denied = client.get("/api/v1/devices", headers={"host": "evil.example"})
        assert denied.status_code == 404
        # 共享路径（health/version）对 loopback 始终可用
        assert client.get("/api/v1/health", headers={"host": "localhost"}).status_code == 200


def test_http_carrier_roundtrip(tmp_dir: Path) -> None:
    """HTTP 载体：hello → welcome（spec §5.6 线上格式）。

    v3 link 只认 DB 里的 token_hash（server-api W1 语义），因此这里直接造 devices 行，
    不走 legacy enroll（避免把 0.2.x 会话塞进 v3 registry）。
    """

    from tests.helpers import seed_device

    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-V3-CARRIER", token="v3-carrier-token")
        token = "v3-carrier-token"
        hello = {
            "v": 3,
            "id": "uplink-hello-1",
            "t": "hello",
            "ts_ms": 1_730_000_000_000,
            "body": {
                "agent_version": "0.3.0",
                "protocol": 3,
                "boot_id": "boot-1",
                "device_uid": "OBS-V3-CARRIER",
                "last_ack_id": "",
                "device_time_ms": 1_730_000_000_000,
            },
        }
        response = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": token},
            json={"v": 3, "last_ack_id": "", "items": [hello]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["v"] == 3
        assert set(body) >= {"v", "ack_id", "server_time_ms", "items"}
        # ack_id 是服务端最后确认的**上行** id
        assert body["ack_id"] == "uplink-hello-1"
        assert isinstance(body["items"], list) and body["items"], "首帧应返回 welcome"
        welcome = body["items"][0]
        assert welcome["t"] == "welcome"
        assert welcome["v"] == 3
        assert set(welcome["body"]) >= {"server_version", "heartbeat_interval_s", "server_time_ms"}
        # server-api W1：welcome.config 非空，且来自 device_safe_config（无 secret）
        config = welcome["body"]["config"]
        assert isinstance(config, dict) and config, "welcome.config 不能是空对象"
        assert "archive_token" not in config

        # welcome 之后 device 侧应能继续普通上行（event/heartbeat 不要求 hello）
        event = {
            "v": 3,
            "id": "uplink-event-1",
            "t": "event",
            "ts_ms": 1_730_000_000_100,
            "body": {"event": "transport", "data": {"level": "ws", "connected": True}},
        }
        heartbeat = {
            "v": 3,
            "id": "uplink-heartbeat-1",
            "t": "heartbeat",
            "ts_ms": 1_730_000_000_200,
            "body": {
                "agent_version": "0.3.0",
                "boot_id": "boot-1",
                "device_time_ms": 1_730_000_000_200,
                "state": {"transport": "http", "connected": True},
            },
        }
        merged = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": token},
            json={"v": 3, "last_ack_id": "", "items": [event, heartbeat]},
        )
        assert merged.status_code == 200
        assert merged.json()["ack_id"] == "uplink-heartbeat-1"


def test_link_auth_semantics(tmp_dir: Path) -> None:
    """server-api W1：无效/缺失 token→401 AUTH；已卸载→410 GONE；换发后旧 token→401。"""

    from sqlalchemy import select

    from observatory.core.clock import now_ms
    from observatory.core.models import Device
    from tests.helpers import seed_device

    batch = {"v": 3, "last_ack_id": "", "items": []}

    def hello_batch(index: int) -> dict:
        return {
            "v": 3,
            "last_ack_id": "",
            "items": [
                {
                    "v": 3,
                    "id": f"uplink-hello-auth-{index}",
                    "t": "hello",
                    "ts_ms": 1_730_000_000_000 + index,
                    "body": {
                        "agent_version": "0.3.0",
                        "protocol": 3,
                        "boot_id": f"boot-auth-{index}",
                        "device_uid": "OBS-V3-AUTH",
                        "last_ack_id": "",
                        "device_time_ms": 1_730_000_000_000 + index,
                    },
                }
            ],
        }

    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-V3-AUTH", token="v3-auth-token")

        missing = client.post("/api/v1/agent/link", json=batch)
        assert missing.status_code == 401
        assert missing.json()["code"] == "AUTH"
        assert missing.json()["retryable"] is False

        invalid = client.post("/api/v1/agent/link", headers={"X-Device-Token": "not-a-token"}, json=batch)
        assert invalid.status_code == 401

        valid = client.post("/api/v1/agent/link", headers={"X-Device-Token": "v3-auth-token"}, json=hello_batch(1))
        assert valid.status_code == 200, valid.text

        # token 轮换（re-enroll 语义）：旧 token 立即失效
        seed_device(client, uid="OBS-V3-AUTH", token="v3-auth-token-2")
        rotated = client.post("/api/v1/agent/link", headers={"X-Device-Token": "v3-auth-token"}, json=batch)
        assert rotated.status_code == 401, "re-enroll 覆盖 token_hash 后旧 token 必须 401"
        rotated_new = client.post(
            "/api/v1/agent/link", headers={"X-Device-Token": "v3-auth-token-2"}, json=hello_batch(2)
        )
        assert rotated_new.status_code == 200

        async def mark_uninstalled() -> None:
            async with client.app.state.db.sessionmaker() as session:
                row = (await session.execute(select(Device).where(Device.uid == "OBS-V3-AUTH"))).scalars().first()
                assert row is not None
                row.uninstalled_at_ms = now_ms()
                await session.commit()

        client.portal.call(mark_uninstalled)
        gone = client.post("/api/v1/agent/link", headers={"X-Device-Token": "v3-auth-token-2"}, json=batch)
        assert gone.status_code == 410
        assert gone.json()["code"] == "GONE"


def test_registry_has_single_session_type_after_legacy_and_v3(tmp_dir: Path) -> None:
    """混合载体回归（Lead 要求）：registry 只能有 AgentSession，legacy 端点仍可用。

    0.2.x enroll 先注册 protocol=2 的 AgentSession；v3 link 用同一 token 升级会话后，
    旧 heartbeat 端点必须继续工作，且不能再出现第二种会话类型（C1）。
    """

    from observatory.link.session import AgentSession
    from tests.contract.fixtures import agent_0_2_10 as legacy

    with running_app(tmp_dir) as client:
        token = legacy_token(client)
        hello = {
            "v": 3,
            "id": "mix-1",
            "t": "hello",
            "ts_ms": 1_730_000_000_000,
            "body": {
                "agent_version": "0.3.0",
                "protocol": 3,
                "boot_id": "mix-boot",
                "device_uid": "legacy-uid",
                "last_ack_id": "",
                "device_time_ms": 1_730_000_000_000,
            },
        }
        upgraded = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": token},
            json={"v": 3, "last_ack_id": "", "items": [hello]},
        )
        assert upgraded.status_code == 200, upgraded.text
        assert upgraded.json()["items"][0]["t"] == "welcome"

        sessions = client.app.state.links.sessions()
        assert sessions, "v3 link 后 registry 里必须有会话"
        assert all(isinstance(session, AgentSession) for session in sessions), [
            type(session).__name__ for session in sessions
        ]
        assert any(int(getattr(session, "protocol", 0)) == 3 for session in sessions), "v3 会话必须标记 protocol=3"

        heartbeat = client.post(
            "/api/v1/agent/heartbeat",
            headers={"X-Device-Token": token},
            json=legacy.HEARTBEAT_REQUEST,
        )
        assert heartbeat.status_code == 200, heartbeat.text
        assert isinstance(heartbeat.json().get("config"), dict)


def test_fake_legacy_agent_roundtrip(tmp_dir: Path) -> None:
    """N2 的集成形态：0.2.10 真实报文在当前服务端上能完整跑一轮。"""

    from PIL import Image

    from tests.contract.fixtures import agent_0_2_10 as legacy

    with running_app(tmp_dir) as client:
        enroll = client.post("/api/v1/agent/enroll", json=legacy.enroll_request(code="test-enroll-code"))
        assert enroll.status_code == 200, enroll.text
        enroll_body = enroll.json()
        assert set(enroll_body) >= legacy.ENROLL_RESPONSE_REQUIRED_KEYS
        token = enroll_body["token"]
        headers = {legacy.DEVICE_TOKEN_HEADER: token}

        heartbeat = client.post("/api/v1/agent/heartbeat", headers=headers, json=legacy.HEARTBEAT_REQUEST)
        assert heartbeat.status_code == 200, heartbeat.text
        assert set(heartbeat.json()) >= legacy.HEARTBEAT_RESPONSE_REQUIRED_KEYS

        polled = client.get("/api/v1/agent/commands", params={"wait": 0}, headers=headers)
        assert polled.status_code == 200
        assert set(polled.json()) >= legacy.POLL_RESPONSE_REQUIRED_KEYS

        buffer = io.BytesIO()
        Image.new("RGB", (64, 48), (10, 200, 30)).save(buffer, format="WEBP")
        frame = client.post(
            "/api/v1/agent/frames",
            params=legacy.FRAME_UPLOAD_QUERY,
            headers={**headers, **legacy.FRAME_UPLOAD_HEADERS},
            content=buffer.getvalue(),
        )
        assert frame.status_code == 200, frame.text
        assert set(frame.json()) >= legacy.FRAME_UPLOAD_RESPONSE_REQUIRED_KEYS

        state = client.get("/api/v1/agent/state", headers=headers)
        assert state.status_code == 200
        assert {"presence", "server_version", "server_time_ms"} <= set(state.json())

        # 401 分支（旧端只依据状态码清 token 重新 enroll；0.3.0 错误体已统一为
        # {code,message,retryable}，因此这里同时接受新体和旧端夹具里的 {detail}）
        denied = client.post(
            "/api/v1/agent/heartbeat",
            headers={legacy.DEVICE_TOKEN_HEADER: "invalid"},
            json=legacy.HEARTBEAT_REQUEST,
        )
        assert denied.status_code == 401
        denied_body = denied.json()
        message = denied_body.get("detail") or denied_body.get("message") or ""
        assert message == "设备令牌无效", denied_body