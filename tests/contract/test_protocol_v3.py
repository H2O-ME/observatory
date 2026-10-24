"""协议 v3 契约（spec §5）：端点、信封方向、幂等、C1/C2/C7 结构约束。

W0：断言已写死，W1 起逐条解除 skip。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_SRC = REPO_ROOT / "server/observatory"

DOWNLINK_TYPES = {"welcome", "config", "command", "control", "ping", "pong", "uninstall", "update_offer"}
UPLINK_TYPES = {"hello", "heartbeat", "result", "event", "control_frame", "uninstall_ack", "pong"}


def _app():
    from observatory.main import create_app  # noqa: PLC0415

    return create_app()


def test_v3_link_endpoints_exist(tmp_dir: Path) -> None:
    """惰性 include_router 下 app.routes 看不到子路由，用真实请求探活。"""

    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_dir) as client:
        link = client.post("/api/v1/agent/link", json={"v": 3, "last_ack_id": "", "items": []})
        assert link.status_code == 401, link.text  # 已注册，只是缺设备令牌
        frames = client.post("/api/v1/agent/frames", content=b"")
        assert frames.status_code in {401, 422}, frames.text


def test_envelope_direction_table_matches_spec() -> None:
    from observatory.link import envelope  # noqa: PLC0415

    assert set(envelope.DOWNLINK_TYPES) == DOWNLINK_TYPES
    assert set(envelope.UPLINK_TYPES) == UPLINK_TYPES


def test_http_carrier_hold_vs_client_timeout() -> None:
    from observatory.core.config import Settings  # noqa: PLC0415

    hold = Settings().poll_hold_s
    assert hold <= 20, "HTTP hold 必须 ≤20s（spec §5.3）"
    assert hold + 10 <= 30, "客户端超时 30s，必须留有 10s 余量"


def test_idempotency_contracts(tmp_dir: Path) -> None:
    """上行 id LRU 去重 + 帧 X-Frame-Id 幂等（through HTTP carrier）。"""

    from io import BytesIO  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    from tests.helpers import running_app, seed_device  # noqa: PLC0415

    event = {
        "v": 3,
        "id": "idem-event-1",
        "t": "event",
        "ts_ms": 1_730_000_000_000,
        "body": {"event": "noop", "data": {}},
    }
    buffer = BytesIO()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(buffer, format="WEBP")
    frame_headers = {"X-Device-Token": "idem-token", "X-Frame-Id": "idem-frame-1"}
    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-IDEM", token="idem-token")
        for _ in range(2):
            response = client.post(
                "/api/v1/agent/link",
                headers={"X-Device-Token": "idem-token"},
                json={"v": 3, "last_ack_id": "", "items": [event]},
            )
            assert response.status_code == 200
        assert client.app.state.links.sessions()[0].stats()["duplicate"] >= 1

        first = client.post("/api/v1/agent/frames", params={"device_ts_ms": 1_730_000_000_000}, headers=frame_headers, content=buffer.getvalue())
        second = client.post("/api/v1/agent/frames", params={"device_ts_ms": 1_730_000_000_000}, headers=frame_headers, content=buffer.getvalue())
        assert first.status_code == second.status_code == 200
        assert first.json()["ts_ms"] == second.json()["ts_ms"], "同一 X-Frame-Id 必须幂等"


def test_c1_no_second_heartbeat_or_result_implementation() -> None:
    """C1：代码里搜不到第二套 heartbeat/result HTTP 端点（0.3.1 起 legacy 已删）。"""

    offenders = []
    for path in SERVER_SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r'@router\.(get|post)\("/(heartbeat|commands)', text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"C1 违规: {offenders}"


def test_c2_frames_have_single_ingress() -> None:
    """C2：没有 WS 二进制帧分支（legacy 已删，帧只有 /agent/frames 一条路）。"""

    offenders = []
    for path in SERVER_SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r'kind\s*==\s*"frame"', text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"C2 违规: {offenders}"


def test_c7_legacy_layer_is_gone() -> None:
    """C7（0.3.1 改版）：legacy 兼容层已整体删除，任何代码不得引用。"""

    assert not (SERVER_SRC / "legacy").exists(), "legacy/ 目录应已删除"
    offenders = []
    for path in SERVER_SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"from\s+\.+legacy|import\s+observatory\.legacy|agent_v2", text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"C7 违规（legacy 残留引用）: {offenders}"


def test_c4_registry_not_read_outside_presence() -> None:
    # 链路载体可以按 device_id 找 session（这是传输层路由，不是"在线"判定）；
    # 任何 console/业务代码不得直接读 registry。
    allowed = {"presence.py", "registry.py", "session.py", "carrier_http.py", "carrier_ws.py"}
    offenders = []
    for path in SERVER_SRC.rglob("*.py"):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"registry\.(get|attach|detach)\(", text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"C4 违规: {offenders}"


# ---------------------------------------------------------------------------
# §5.6 线上格式（权威）
# ---------------------------------------------------------------------------


def _enrolled_token(client) -> str:
    """v3 契约测试用 seed_device 直接落库，避免 legacy enroll 先建 protocol2 会话。"""

    from tests.helpers import seed_device  # noqa: PLC0415

    seed_device(client, uid="OBS-V3-CONTRACT", token="v3-contract-token")
    return "v3-contract-token"


def test_ack_id_is_last_uplink_id(tmp_dir: Path) -> None:
    """ack_id = 服务端已处理的最后一条**上行** id；不是下行 id。"""

    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_dir) as client:
        token = _enrolled_token(client)
        items = [
            {
                "v": 3,
                "id": uplink_id,
                "t": "event",
                "ts_ms": 1_730_000_000_000,
                "body": {"event": "noop", "data": {"seq": index}},
            }
            for index, uplink_id in enumerate(("a", "b", "c"))
        ]
        response = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": token},
            json={"v": 3, "last_ack_id": "", "items": items},
        )
        assert response.status_code == 200, response.text
        assert response.json()["ack_id"] == "c"


def test_last_ack_id_suppresses_downlink_redelivery(tmp_dir: Path) -> None:
    """请求带 last_ack_id=<下行 id> 时，服务端不再重发该下行。"""

    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_dir) as client:
        token = _enrolled_token(client)
        headers = {"X-Device-Token": token}
        hello = {
            "v": 3,
            "id": "suppress-hello",
            "t": "hello",
            "ts_ms": 1_730_000_000_000,
            "body": {
                "agent_version": "0.3.0",
                "protocol": 3,
                "boot_id": "suppress-boot",
                "device_uid": "OBS-V3-CONTRACT",
                "last_ack_id": "",
                "device_time_ms": 1_730_000_000_000,
            },
        }
        first = client.post("/api/v1/agent/link", headers=headers, json={"v": 3, "last_ack_id": "", "items": [hello]})
        assert first.status_code == 200
        downlink_ids = [item["id"] for item in first.json()["items"]]
        assert downlink_ids, "hello 后的首帧必须带 welcome（下行）"
        confirmed = downlink_ids[0]
        heartbeat = {
            "v": 3,
            "id": "suppress-heartbeat",
            "t": "heartbeat",
            "ts_ms": 1_730_000_000_100,
            "body": {"agent_version": "0.3.0", "boot_id": "suppress-boot", "state": {"transport": "http"}},
        }
        second = client.post(
            "/api/v1/agent/link",
            headers=headers,
            json={"v": 3, "last_ack_id": confirmed, "items": [heartbeat]},
        )
        assert second.status_code == 200
        assert confirmed not in [item["id"] for item in second.json()["items"]]


def test_v3_frame_upload_contract(tmp_dir: Path) -> None:
    """帧上传：device_ts_ms 查询参数 + X-Frame-Id + image/jpeg；重复 id 返回同一 ts_ms。"""

    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    from tests.helpers import running_app  # noqa: PLC0415

    buffer = io.BytesIO()
    Image.new("RGB", (320, 200), (32, 64, 96)).save(buffer, format="JPEG", quality=80)
    body = buffer.getvalue()

    with running_app(tmp_dir) as client:
        token = _enrolled_token(client)
        headers = {
            "X-Device-Token": token,
            "X-Frame-Id": "frame-uuid-1",
            "Content-Type": "image/jpeg",
        }
        params = {"device_ts_ms": 1_730_000_000_000, "session_id": 1, "kind": "screen"}
        first = client.post("/api/v1/agent/frames", params=params, headers=headers, content=body)
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert set(first_body) >= {"frame_id", "ts_ms", "stored", "server_time_ms"}
        assert first_body["stored"] is True
        # 同一个 X-Frame-Id 重传：返回同一 ts_ms，不重复入库/转码
        again = client.post("/api/v1/agent/frames", params=params, headers=headers, content=body)
        assert again.status_code == 200
        assert again.json()["ts_ms"] == first_body["ts_ms"]