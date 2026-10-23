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


@pytest.mark.skip(reason="W1: 等待 link/carrier_http.py 的 POST /api/v1/agent/link")
def test_v3_link_endpoints_exist() -> None:
    app = _app()
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/v1/agent/link" in paths
    assert "/api/v1/agent/frames" in paths


@pytest.mark.skip(reason="W1: 等待 link/envelope.py 的 DIRECTION 表")
def test_envelope_direction_table_matches_spec() -> None:
    from observatory.link import envelope  # noqa: PLC0415

    assert set(envelope.DOWNLINK_TYPES) == DOWNLINK_TYPES
    assert set(envelope.UPLINK_TYPES) == UPLINK_TYPES


@pytest.mark.skip(reason="W1: 等待 HTTP 载体的 hold 语义（spec §5.3：hold≤20s，客户端 30s）")
def test_http_carrier_hold_vs_client_timeout() -> None:
    from observatory.link.carrier_http import HOLD_S  # noqa: PLC0415

    assert HOLD_S <= 20
    assert HOLD_S + 10 <= 30  # 客户端超时 30s（spec §5.3）


@pytest.mark.skip(reason="W1: 等待幂等实现（上行 id 5 分钟 LRU / frame X-Frame-Id）")
def test_idempotency_contracts() -> None:
    from observatory.link.session import UplinkDeduper  # noqa: PLC0415
    from observatory.media.ingest import FrameIdempotency  # noqa: PLC0415

    deduper = UplinkDeduper(window_s=300)
    assert deduper.first_seen("x") and not deduper.first_seen("x")
    gate = FrameIdempotency()
    assert gate.claim("f1") and not gate.claim("f1")


@pytest.mark.skip(reason="W1: 需要 C1 收敛（只有一条 link 实现，无第二套 heartbeat/result）")
def test_c1_no_second_heartbeat_or_result_implementation() -> None:
    """C1：除 legacy/ 外，代码里搜不到第二套 heartbeat/result HTTP 端点。"""

    offenders = []
    for path in SERVER_SRC.rglob("*.py"):
        if "legacy" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r'@router\.(get|post)\("/(heartbeat|commands)', text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"C1 违规（旧端点必须在 legacy/ 内）: {offenders}"


@pytest.mark.skip(reason="W1: 需要 C2 收敛（帧只走 POST /api/v1/agent/frames）")
def test_c2_frames_have_single_ingress() -> None:
    """C2：新代码里没有 WS 二进制帧分支（legacy/ 除外）。"""

    offenders = []
    for path in SERVER_SRC.rglob("*.py"):
        if "legacy" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r'kind\s*==\s*"frame"', text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"C2 违规: {offenders}"


def test_c7_legacy_is_imported_only_by_assembly() -> None:
    offenders = []
    for path in SERVER_SRC.rglob("*.py"):
        if "legacy" in path.parts or path.name == "main.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"from\s+\.+legacy|import\s+observatory\.legacy", text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"C7 违规: {offenders}"


def test_c4_registry_not_read_outside_presence() -> None:
    allowed = {"presence.py", "registry.py", "session.py"}
    offenders = []
    for path in SERVER_SRC.rglob("*.py"):
        if path.name in allowed or "legacy" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"registry\.(get|attach|detach)\(", text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"C4 违规: {offenders}"


# ---------------------------------------------------------------------------
# §5.6 线上格式（权威）
# ---------------------------------------------------------------------------


def _enrolled_token(client) -> str:
    from tests.contract.fixtures import agent_0_2_10 as legacy  # noqa: PLC0415

    response = client.post("/api/v1/agent/enroll", json=legacy.enroll_request(code="test-enroll-code"))
    assert response.status_code == 200, response.text
    return response.json()["token"]


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
        first = client.post("/api/v1/agent/link", headers=headers, json={"v": 3, "last_ack_id": "", "items": []})
        assert first.status_code == 200
        downlink_ids = [item["id"] for item in first.json()["items"]]
        if not downlink_ids:
            # 新会话首帧必带 welcome；没有下行说明实现退化，直接失败而不是 skip
            pytest.fail("首帧没有 welcome/下行，无法验证 last_ack_id 抑制重投")
        confirmed = downlink_ids[0]
        second = client.post(
            "/api/v1/agent/link",
            headers=headers,
            json={"v": 3, "last_ack_id": confirmed, "items": []},
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