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
    """HTTP 载体：hello → welcome（spec §5.6 线上格式）。"""

    with running_app(tmp_dir) as client:
        token = legacy_token(client)
        hello = {
            "v": 3,
            "id": "uplink-hello-1",
            "t": "hello",
            "ts_ms": 1_730_000_000_000,
            "body": {
                "agent_version": "0.3.0",
                "protocol": 3,
                "boot_id": "boot-1",
                "device_uid": "OBS-TEST",
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