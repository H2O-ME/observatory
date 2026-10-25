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

# ---------------------------------------------------------------------------
# 设备生命周期（F9 修订，2026-10-25 生产事故）：删除 → 重装 → 重新接入
# ---------------------------------------------------------------------------

_NOOP_ITEMS = [
    {
        "v": 3,
        "id": "resurrection-probe",
        "t": "event",
        "ts_ms": 1_790_000_000_000,
        "body": {"event": "noop", "data": {}},
    }
]


def _admin_headers(client) -> dict[str, str]:
    from tests.helpers import TEST_ADMIN_PASSWORD  # noqa: PLC0415

    response = client.post("/api/v1/auth/login", json={"password": TEST_ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['token']}"}


def _mint(client, headers: dict[str, str]) -> str:
    response = client.post("/api/v1/install-tokens", json={"ttl_s": 600}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]["token"]


def _enroll(client, uid: str, install_token: str):
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


def _link(client, device_token: str):
    return client.post(
        "/api/v1/agent/link",
        headers={"X-Device-Token": device_token},
        json={"v": 3, "last_ack_id": "", "items": _NOOP_ITEMS},
    )


def _age_out(tmp_dir: Path, uid: str) -> None:
    """enroll 会把 last_seen_ms 写成当下，删除端点会拒绝在线设备；
    用同步 sqlite3 把该行改老（WAL 允许与 app 的异步连接并发短写）。"""

    import sqlite3  # noqa: PLC0415
    import time  # noqa: PLC0415

    connection = sqlite3.connect(tmp_dir / "observatory.db")
    try:
        connection.execute(
            "UPDATE devices SET last_seen_ms = ? WHERE uid = ?",
            (int(time.time() * 1000) - 600_000, uid),
        )
        connection.commit()
    finally:
        connection.close()


def test_delete_then_reinstall_with_fresh_token_reenrolls(tmp_dir: Path) -> None:
    """一次性安装令牌 = 管理员重新接入授权：可击穿删除墓碑复活 uid。"""

    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_dir) as client:
        headers = _admin_headers(client)

        first = _enroll(client, "OBS-REBORN-1", _mint(client, headers))
        assert first.status_code == 200, first.text
        old_device_token = first.json()["token"]

        _age_out(tmp_dir, "OBS-REBORN-1")
        assert client.delete("/api/v1/devices/OBS-REBORN-1", headers=headers).status_code == 200
        assert _link(client, old_device_token).status_code == 410

        second = _enroll(client, "OBS-REBORN-1", _mint(client, headers))
        assert second.status_code == 200, second.text
        new_device_token = second.json()["token"]
        assert new_device_token != old_device_token

        audit = client.get("/api/v1/audit?limit=50", headers=headers)
        enrolls = [
            entry
            for entry in audit.json()["data"]["entries"]
            if entry.get("action") == "device.enroll" and entry.get("device_uid") == "OBS-REBORN-1"
        ]
        assert [entry["detail"].get("resurrected") for entry in enrolls] == [True, False]

        # 墓碑已随复活烧掉：旧 token 401（hash 未知），新 token 正常上线
        assert _link(client, old_device_token).status_code == 401
        assert _link(client, new_device_token).status_code == 200


def test_used_token_cannot_resurrect_tombstoned_uid(tmp_dir: Path) -> None:
    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_dir) as client:
        headers = _admin_headers(client)

        consumed = _mint(client, headers)
        assert _enroll(client, "OBS-TOMB-A", consumed).status_code == 200

        assert _enroll(client, "OBS-TOMB-B", _mint(client, headers)).status_code == 200
        _age_out(tmp_dir, "OBS-TOMB-B")
        assert client.delete("/api/v1/devices/OBS-TOMB-B", headers=headers).status_code == 200

        response = _enroll(client, "OBS-TOMB-B", consumed)
        assert response.status_code == 401, response.text
        assert "删除" in response.text


def test_tombstoned_uid_with_invalid_token_still_rejected(tmp_dir: Path) -> None:
    from tests.helpers import running_app  # noqa: PLC0415

    with running_app(tmp_dir) as client:
        headers = _admin_headers(client)

        assert _enroll(client, "OBS-TOMB-C", _mint(client, headers)).status_code == 200
        _age_out(tmp_dir, "OBS-TOMB-C")
        assert client.delete("/api/v1/devices/OBS-TOMB-C", headers=headers).status_code == 200

        assert _enroll(client, "OBS-TOMB-C", "not-a-real-token").status_code == 401


# ---------------------------------------------------------------------------
# files 载荷解包（2026-10-25 生产：驱动器/目录列表恒空、读文件恒报无法解码）
# ---------------------------------------------------------------------------

_DRIVE = {
    "name": "本地磁盘 (C:)",
    "path": "C:\\",
    "is_dir": True,
    "size": 53684989952,
    "mtime_ms": 0,
    "hidden": False,
    "drive": "C:",
    "free": 42551586816,
    "filesystem": "NTFS",
}


def test_files_payload_unwrap() -> None:
    """受控端结果带 1-2 层 {ok, op, data{...}} 包装，必须剥到载荷本体。"""

    from observatory.console.files import _unwrap_agent_payload  # noqa: PLC0415

    double = {
        "ok": True,
        "op": "drives",
        "data": {
            "ok": True,
            "op": "drives",
            "data": {"drives": [_DRIVE], "server_time_ms": 1},
            "error": None,
            "elapsed_ms": 3,
        },
        "error": None,
        "elapsed_ms": 295,
    }
    assert _unwrap_agent_payload(double)["drives"] == [_DRIVE]

    single = {
        "ok": True,
        "op": "list",
        "data": {"path": "C:\\", "parent": None, "entries": [], "truncated": False, "drives": None},
    }
    assert _unwrap_agent_payload(single)["path"] == "C:\\"

    # read 的 data 字段是 base64 字符串：不是 dict，循环立即停止，不得误剥
    read_chunk = {"path": "C:\a.bin", "data": "QUJD", "size": 3, "offset": 0, "eof": True}
    assert _unwrap_agent_payload(read_chunk) is read_chunk

    plain = {"path": "C:\a.bin", "sha256": "ab" * 32, "size": 1}
    assert _unwrap_agent_payload(plain) is plain
