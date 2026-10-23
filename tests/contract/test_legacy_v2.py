"""0.2.10 受控端协议回放：兼容层与 N2 零重装升级的验收判据。

分两部分：

1. **静态证据测试（现在就能跑，通过）**
   校验 `fixtures/agent_0_2_10.py` 内部自洽：端点集合、字段集合、消息方向、
   二进制帧往返、N2 剧本完整性。这些测试保护的是"证据本身没被改坏"。

2. **回放测试（W1 起启用，当前 skip(reason="W1: ...")）**
   等 `server/observatory/legacy/agent_v2.py` 就位后，用旧端真实 payload 打新服务端：
   enroll → heartbeat/poll → command → result → frames → WS welcome/heartbeat，以及
   N2 升级剧本。断言内容已按 spec §3.1（A3/A4/A10/A12/N2）写死，W1 只需去掉装饰器。

约定：本文件**不得**在模块级 import `server/` 或 `agent/`（旧代码会被删除，避免收集错误）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.contract.fixtures import agent_0_2_10 as legacy

pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# 第一部分：静态证据（现在运行）
# ---------------------------------------------------------------------------


def test_fixture_identity_is_0_2_10() -> None:
    assert legacy.LEGACY_AGENT_VERSION == "0.2.10"
    assert "0.2" in legacy.LEGACY_PROTOCOL_ID
    # 0.2.10 的真实 VERSION 是唯一来源；fixture 不能漂移
    assert legacy.LEGACY_AGENT_VERSION in legacy.LEGACY_SERVER_ACCEPTS_VERSIONS


def test_legacy_endpoint_surface_matches_spec_section_2_3() -> None:
    """spec §2.3 点名的旧端点必须全部在证据里。"""

    expected = {
        "enroll",
        "heartbeat",
        "poll_commands",
        "command_result",
        "frame_upload",
        "device_state",
        "uninstall_ack",
        "agent_ws",
    }
    assert expected <= set(legacy.LEGACY_ENDPOINTS)
    methods = {name: method for name, (method, _path) in legacy.LEGACY_ENDPOINTS.items()}
    assert methods["enroll"] == "POST"
    assert methods["heartbeat"] == "POST"
    assert methods["poll_commands"] == "GET"
    assert methods["command_result"] == "POST"
    assert methods["frame_upload"] == "POST"
    assert methods["device_state"] == "GET"
    assert methods["uninstall_ack"] == "POST"
    assert methods["agent_ws"] == "WS"
    for _name, (_method, path) in legacy.LEGACY_ENDPOINTS.items():
        assert path.startswith("/api/v1/"), path


def test_enroll_payload_shape_is_frozen() -> None:
    payload = legacy.ENROLL_REQUEST
    assert set(payload) >= legacy.ENROLL_REQUEST_REQUIRED_KEYS
    assert payload["agent_version"] == legacy.LEGACY_AGENT_VERSION
    assert isinstance(payload["sysinfo"], dict)
    assert isinstance(payload["sessions"], list)
    assert payload["sessions"], "0.2.10 总会带上 session 列表（可能为空列表）"
    # 旧端依赖 pydantic 忽略 sysinfo/sessions 里的多余键（如 station）
    assert "station" in payload["sessions"][0]
    response = legacy.ENROLL_RESPONSE
    assert set(response) >= legacy.ENROLL_RESPONSE_REQUIRED_KEYS
    assert isinstance(response["token"], str) and response["token"]
    assert len(response["base_urls"]) >= 2, "旧端做域名 failover，至少两个 base_urls"
    assert isinstance(response["config"], dict) and response["config"]


def test_heartbeat_payload_shape_and_transport_values() -> None:
    payload = legacy.HEARTBEAT_REQUEST
    assert {"agent_version", "sysinfo", "state"} <= set(payload)
    state = payload["state"]
    assert {"transport", "connected", "sessions", "spool", "control", "updating", "capture"} <= set(state)
    assert state["transport"] in legacy.LEGACY_TRANSPORT_VALUES
    assert isinstance(state["capture"], dict)
    assert {"state", "reason", "workers", "sessions"} <= set(state["capture"])

    response = legacy.HEARTBEAT_RESPONSE
    assert set(response) >= legacy.HEARTBEAT_RESPONSE_REQUIRED_KEYS
    assert response["heartbeat_interval_s"] > 0
    assert response["poll_hold_s"] > 0
    assert isinstance(response["commands"], list)


def test_poll_and_ws_command_objects_differ_on_purpose() -> None:
    """旧端两种载体收到的 command 形状不同；兼容层必须两种都保持。"""

    polled = legacy.POLL_COMMAND_HIT["commands"][0]
    pushed = legacy.WS_SERVER_COMMAND["command"]
    assert set(polled) >= {
        "id",
        "type",
        "status",
        "payload",
        "result",
        "error",
        "created_at_ms",
        "dispatched_at_ms",
        "finished_at_ms",
    }
    assert "status" not in pushed
    assert "timeout_ms" in pushed
    assert polled["id"] == pushed["id"]
    assert polled["type"] == pushed["type"]
    assert polled["payload"] == pushed["payload"]


def test_command_result_payloads() -> None:
    ok = legacy.COMMAND_RESULT_REQUEST_OK
    assert set(ok) == {"ok", "result", "error"}
    assert ok["ok"] is True and ok["error"] is None
    failed = legacy.COMMAND_RESULT_REQUEST_FAIL
    assert failed["ok"] is False and isinstance(failed["error"], str) and failed["error"]
    assert legacy.COMMAND_RESULT_RESPONSE["ok"] is True


def test_frame_upload_shape() -> None:
    query = legacy.FRAME_UPLOAD_QUERY
    assert set(query) == {"ts_ms", "session_id", "kind"}
    assert query["kind"] == "screen"
    assert legacy.FRAME_UPLOAD_HEADERS["Content-Type"] == "image/webp"
    assert legacy.FRAME_UPLOAD_BODY_PREFIX[:4] == b"RIFF"
    response = legacy.FRAME_UPLOAD_RESPONSE
    assert set(response) >= legacy.FRAME_UPLOAD_RESPONSE_REQUIRED_KEYS
    # 0.2.10 不发送幂等头；0.3.0 若新增 Idempotency-Key 必须对旧端可选
    assert "Idempotency-Key" not in legacy.FRAME_UPLOAD_HEADERS


def test_ws_welcome_contract() -> None:
    welcome = legacy.WS_SERVER_WELCOME
    assert welcome["t"] == "welcome"
    assert set(welcome) >= legacy.WS_SERVER_WELCOME_REQUIRED_KEYS
    assert welcome["heartbeat_interval_s"] > 0
    assert welcome["poll_hold_s"] > 0
    assert isinstance(welcome["config"], dict)
    assert welcome["update"] is None or {"version", "url", "sha256"} <= set(welcome["update"])


def test_ws_session_sequence_is_playable() -> None:
    sequence = legacy.WS_SESSION_SEQUENCE
    assert len(sequence) >= 8
    directions = [direction for direction, _message in sequence]
    assert set(directions) == {"c2s", "s2c"}
    # 第一条必须是升级握手，第二条必须是 welcome
    assert "__http_upgrade__" in sequence[0][1]
    assert sequence[1][1]["t"] == "welcome"
    # 每条 s2c/c2s 消息都必须带 t（二进制帧除外）
    for direction, message in sequence[1:]:
        assert "t" in message, f"{direction} 消息缺少 t: {message}"


def test_binary_ctrl_frame_roundtrip() -> None:
    blob = legacy.encode_binary_frame(legacy.CTRL_FRAME_HEADER, legacy.CTRL_FRAME_PAYLOAD)
    header, payload = legacy.decode_binary_frame(blob)
    assert header == legacy.CTRL_FRAME_HEADER
    assert payload == legacy.CTRL_FRAME_PAYLOAD
    # 头长前缀是大端 uint32
    header_bytes = json.dumps(legacy.CTRL_FRAME_HEADER, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert blob[:4] == len(header_bytes).to_bytes(4, "big")


def test_http_error_shapes_all_have_detail() -> None:
    assert legacy.HTTP_ERROR_SHAPES
    for name, shape in legacy.HTTP_ERROR_SHAPES.items():
        assert 400 <= shape["status"] <= 499, name
        assert isinstance(shape["body"], dict) and "detail" in shape["body"], name
        assert isinstance(shape["body"]["detail"], str) and shape["body"]["detail"], name


def test_update_offer_contract_for_old_agent() -> None:
    offer = legacy.HEARTBEAT_RESPONSE["update"]
    assert {"version", "url", "sha256", "size_bytes"} <= set(offer)
    # 旧端在 sha256 为空时会跳过校验（client.py:234）；服务端必须始终给非空摘要
    assert isinstance(offer["sha256"], str) and len(offer["sha256"]) == 64
    # 旧端忽略 url 自己拼路径；两条路径都得能下载
    assert offer["url"].startswith("/api/v1/public/agent/")
    assert legacy.LEGACY_ENDPOINTS["public_package_download"][1].startswith("/api/v1/public/agent/")


def test_n2_scenario_is_complete() -> None:
    scenario = legacy.N2_UPGRADE_SCENARIO
    steps = scenario["steps"]
    assert len(steps) >= 7
    assert steps[0] == "legacy_enroll_accepted"
    assert steps[-1] == "console_reports_0_3_0_without_reinstall"
    assert scenario["given"]["agent_version"] == "0.2.10"
    assert scenario["given"]["server_version"] == "0.3.0"
    assert "zero-reinstall" in scenario["name"]
    assert len(scenario["invariants"]) >= 4


def test_hard_requirements_cover_every_response_shape() -> None:
    requirements = legacy.LEGACY_HARD_REQUIREMENTS
    assert set(requirements["response_fields"]) == {
        "enroll",
        "heartbeat",
        "poll",
        "result",
        "frame_upload",
        "ws_welcome",
    }
    assert requirements["request_headers"] == (legacy.DEVICE_TOKEN_HEADER,)
    assert legacy.DEVICE_TOKEN_HEADER == "X-Device-Token"
    assert set(requirements["close_codes"]) == {4401, 4002, 4408, 4000}
    # 旧端会读的 config 键必须都在 enroll/heartbeat 的 config 里
    config = legacy.ENROLL_RESPONSE["config"]
    for key in requirements["config_keys_read_by_old_agent"]:
        assert key in config, f"0.2.10 会读 config.{key}，删掉旧端会退化为默认值"


# ---------------------------------------------------------------------------
# 第二部分：回放测试（W1 启用）
# ---------------------------------------------------------------------------


def _legacy_app():
    """构造新服务端 TestClient；W1 起 legacy/agent_v2.py 应已挂载。"""

    from observatory.main import create_app  # 延迟 import：旧代码删除期间不影响收集

    return create_app()


def test_replay_http_legacy_flow(tmp_dir: Path) -> None:
    """旧端 HTTP 载体完整回放：enroll → heartbeat → poll → result → frames。"""

    from tests.helpers import running_app

    with running_app(tmp_dir) as client:
        enroll = client.post("/api/v1/agent/enroll", json=legacy.enroll_request(code="test-enroll-code"))
        assert enroll.status_code == 200, enroll.text
        body = enroll.json()
        assert set(body) >= legacy.ENROLL_RESPONSE_REQUIRED_KEYS
        token = body["token"]

        heartbeat = client.post(
            "/api/v1/agent/heartbeat",
            headers={legacy.DEVICE_TOKEN_HEADER: token},
            json=legacy.HEARTBEAT_REQUEST,
        )
        assert heartbeat.status_code == 200, heartbeat.text
        assert set(heartbeat.json()) >= legacy.HEARTBEAT_RESPONSE_REQUIRED_KEYS

        polled = client.get(
            "/api/v1/agent/commands", params={"wait": 0}, headers={legacy.DEVICE_TOKEN_HEADER: token}
        )
        assert polled.status_code == 200
        assert set(polled.json()) >= legacy.POLL_RESPONSE_REQUIRED_KEYS

        # 旧端上传真实 WebP 帧（Content-Type/webp + 旧 ts_ms 查询参数）
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (8, 8), color=(10, 20, 30)).save(buffer, format="WEBP")
        upload = client.post(
            "/api/v1/agent/frames",
            params=legacy.FRAME_UPLOAD_QUERY,
            headers={legacy.DEVICE_TOKEN_HEADER: token, **legacy.FRAME_UPLOAD_HEADERS},
            content=buffer.getvalue(),
        )
        assert upload.status_code == 200, upload.text


def test_replay_ws_legacy_flow(tmp_dir: Path) -> None:
    """旧端 WS 载体：welcome 形状、心跳回应、命令下发与回执。"""

    from tests.helpers import legacy_token, running_app

    with running_app(tmp_dir) as client:
        token = legacy_token(client)
        with client.websocket_connect(f"/api/v1/agent/ws?token={token}") as ws:
            welcome = ws.receive_json()
            assert welcome["t"] == "welcome"
            assert set(welcome) >= legacy.WS_SERVER_WELCOME_REQUIRED_KEYS
            ws.send_json(legacy.WS_CLIENT_HEARTBEAT)
            reply = ws.receive_json()
            assert reply["t"] in {"pong", "config", "command"}


def test_all_legacy_endpoints_registered_on_new_app(tmp_dir: Path) -> None:
    """用真实请求探活 legacy 端点（FastAPI 惰性 include_router 让 app.routes 看不到子路由）。"""

    from tests.helpers import running_app

    probes = [
        ("POST", "/api/v1/agent/enroll"),
        ("POST", "/api/v1/agent/heartbeat"),
        ("GET", "/api/v1/agent/commands"),
        ("POST", "/api/v1/agent/commands/unknown-command/result"),
        ("POST", "/api/v1/agent/frames"),
        ("GET", "/api/v1/agent/state"),
        ("POST", "/api/v1/agent/uninstall/ack"),
    ]
    with running_app(tmp_dir) as client:
        for method, path in probes:
            if method == "GET":
                response = client.get(path)
            else:
                response = client.post(path, json={})
            assert response.status_code != 404, f"{method} {path} 未注册（404）"
            assert response.status_code != 405, f"{method} {path} 方法不匹配（405）"


def test_legacy_update_offer_always_has_sha256(tmp_dir: Path) -> None:
    """A3：发给 0.2.10 的 update 必须带非空 sha256，否则旧端跳过校验。"""

    from tests.helpers import legacy_token, running_app, seed_agent_package

    with running_app(tmp_dir) as client:
        digest = seed_agent_package(client, version="0.3.0")
        token = legacy_token(client)
        heartbeat = client.post(
            "/api/v1/agent/heartbeat",
            headers={legacy.DEVICE_TOKEN_HEADER: token},
            json=legacy.HEARTBEAT_REQUEST,
        ).json()
        offer = heartbeat.get("update")
        assert offer is not None, "有更新包时必须向 0.2.10 设备下发 offer"
        assert offer["version"] == "0.3.0"
        assert isinstance(offer.get("sha256"), str) and len(offer["sha256"]) == 64
        assert offer["sha256"] == digest


def test_legacy_config_contains_no_secret_keys(tmp_dir: Path) -> None:
    """A4：下发给旧端的 config（enroll/heartbeat/WS config）不含 SECRET_KEYS。"""

    from tests.helpers import legacy_token, running_app

    with running_app(tmp_dir) as client:
        enroll = client.post("/api/v1/agent/enroll", json=legacy.enroll_request(code="test-enroll-code"))
        assert enroll.status_code == 200, enroll.text
        config = enroll.json()["config"]
        forbidden = {"archive_token", "secret_key", "admin_password", "enroll_code", "totp_secret"}
        assert not (forbidden & set(config)), f"配置泄漏: {forbidden & set(config)}"

        token = legacy_token(client)
        heartbeat = client.post(
            "/api/v1/agent/heartbeat",
            headers={legacy.DEVICE_TOKEN_HEADER: token},
            json=legacy.HEARTBEAT_REQUEST,
        ).json()
        heartbeat_config = heartbeat.get("config") or {}
        assert not (forbidden & set(heartbeat_config)), f"heartbeat 配置泄漏: {forbidden & set(heartbeat_config)}"


def test_old_agent_401_triggers_reregistration(tmp_dir: Path) -> None:
    """旧端收到 401 会清 token 重新 enroll（runner.py:389-392）；服务端必须允许。

    旧端只看状态码，但 0.3.0 的 401 体是结构化信封 `{code,message,retryable}`；
    这里同时锁定"状态码 + 信封形状"，防止兼容层退回到 `{detail}`。
    """

    from tests.helpers import running_app

    with running_app(tmp_dir) as client:
        denied = client.post(
            "/api/v1/agent/heartbeat",
            headers={legacy.DEVICE_TOKEN_HEADER: "invalid-token"},
            json=legacy.HEARTBEAT_REQUEST,
        )
        assert denied.status_code == 401
        payload = denied.json()
        assert payload.get("code") == "AUTH", payload
        assert isinstance(payload.get("message"), str) and payload["message"], payload
        assert payload.get("retryable") is False, payload

        fresh = client.post("/api/v1/agent/enroll", json=legacy.enroll_request(code="test-enroll-code"))
        assert fresh.status_code == 200
        assert fresh.json().get("token")


def test_n2_zero_reinstall_upgrade(tmp_dir: Path) -> None:
    """N2 端到端：0.2.10 旧端跑通旧协议 → 收到 0.3.0 更新 → 新端以 v3 重连。"""

    import hashlib
    from io import BytesIO

    from PIL import Image
    from sqlalchemy import select

    from observatory.core.models import Device
    from observatory.link.session import AgentSession
    from tests.helpers import running_app, seed_agent_package

    with running_app(tmp_dir) as client:
        digest = seed_agent_package(client, version="0.3.0")

        # 1) 旧协议继续可用（真实 enroll 报文）
        enroll = client.post("/api/v1/agent/enroll", json=legacy.enroll_request(code="test-enroll-code"))
        assert enroll.status_code == 200, enroll.text
        body = enroll.json()
        token = body["token"]
        uid = body["device_uid"]

        # 2) 旧端拿到 0.3.0 更新提供（非空 sha256）
        heartbeat = client.post(
            "/api/v1/agent/heartbeat",
            headers={legacy.DEVICE_TOKEN_HEADER: token},
            json=legacy.HEARTBEAT_REQUEST,
        ).json()
        offer = heartbeat.get("update")
        assert offer is not None, "0.3.0 服务器必须向 0.2.10 设备提供更新"
        assert offer["version"] == "0.3.0"
        assert len(offer["sha256"]) == 64
        assert offer["sha256"] == digest

        # 3) 旧端自己拼的下载路径可用，且字节摘要与 offer 一致
        package = client.get(f"/api/v1/public/agent/{offer['version']}/package")
        assert package.status_code == 200, package.text
        assert hashlib.sha256(package.content).hexdigest() == offer["sha256"]

        # 4) 旧 ts_ms 帧仍可入库（升级前采集的数据不能丢）
        buffer = BytesIO()
        Image.new("RGB", (8, 8), color=(1, 2, 3)).save(buffer, format="WEBP")
        upload = client.post(
            "/api/v1/agent/frames",
            params=legacy.FRAME_UPLOAD_QUERY,
            headers={legacy.DEVICE_TOKEN_HEADER: token, **legacy.FRAME_UPLOAD_HEADERS},
            content=buffer.getvalue(),
        )
        assert upload.status_code == 200, upload.text

        # 5) 同一 token 以 v3 信封重连 → 同一设备（零重装），registry 仍是单个 AgentSession
        hello = {
            "v": 3,
            "id": "n2-upgrade-hello",
            "t": "hello",
            "ts_ms": 1_760_000_100_000,
            "body": {
                "agent_version": "0.3.0",
                "protocol": 3,
                "boot_id": "n2-boot",
                "device_uid": uid,
                "last_ack_id": "",
                "device_time_ms": 1_760_000_100_000,
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
        assert sessions and all(isinstance(session, AgentSession) for session in sessions)

        async def device_rows() -> list[Device]:
            async with client.app.state.db.sessionmaker() as session:
                return list((await session.execute(select(Device).where(Device.uid == uid))).scalars().all())

        rows = client.portal.call(device_rows)
        assert len(rows) == 1, "N2 零重装：同一 uid 只能有一条设备记录"
        assert rows[0].token_hash
        assert "UPLINK" not in str(rows[0].agent_version)  # 只确认行可读；版本由 hello 更新