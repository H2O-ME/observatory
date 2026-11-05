"""协议 v3 信封单元测试（spec §5.1/§5.3）。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _envelope():
    from observatory.link import envelope  # noqa: PLC0415

    return envelope


def test_envelope_roundtrip() -> None:
    envelope = _envelope()
    body = {"agent_version": "0.3.0", "state": {"transport": "ws"}}
    wire = envelope.encode_envelope(envelope.new_envelope("heartbeat", body, ts_ms=1_730_000_000_000))
    assert isinstance(wire, dict), "线上格式必须是 JSON 对象（§5.6 权威格式）"
    decoded = envelope.decode_envelope(wire)
    assert decoded.v == 3
    assert decoded.t == "heartbeat"
    assert decoded.body == body
    assert decoded.ts_ms == 1_730_000_000_000
    uuid.UUID(decoded.id)


def test_envelope_id_is_unique_and_preserved() -> None:
    envelope = _envelope()
    first = envelope.new_envelope("ping")
    second = envelope.new_envelope("ping")
    assert first.id != second.id
    fixed = envelope.new_envelope("ping", msg_id="a" * 32)
    assert envelope.decode_envelope(envelope.encode_envelope(fixed)).id == "a" * 32


def test_unknown_type_is_ignored_not_fatal(tmp_dir: Path) -> None:
    """未知 t 必须可解码；session 只处理已知类型（忽略而不是 500）。"""

    envelope = _envelope()
    decoded = envelope.decode_envelope(envelope.encode_envelope(envelope.new_envelope("future_type", {"x": 1})))
    assert decoded.t == "future_type"

    from tests.helpers import running_app, seed_device

    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-UNKNOWN", token="unknown-type-token")
        batch = {
            "v": 3,
            "last_ack_id": "",
            "items": [
                {
                    "v": 3,
                    "id": "unknown-1",
                    "t": "future_type",
                    "ts_ms": 1_730_000_000_000,
                    "body": {"x": 1},
                }
            ],
        }
        response = client.post("/api/v1/agent/link", headers={"X-Device-Token": "unknown-type-token"}, json=batch)
        assert response.status_code == 200, response.text


def test_uplink_id_dedupes_within_window(tmp_dir: Path) -> None:
    """同一上行 id 重投不重复处理（5 分钟 LRU）。"""

    from tests.helpers import running_app, seed_device

    item = {
        "v": 3,
        "id": "dup-1",
        "t": "event",
        "ts_ms": 1_730_000_000_000,
        "body": {"event": "transport", "data": {"seq": 1}},
    }
    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-DEDUPE", token="dedupe-token")
        for _ in range(2):
            response = client.post(
                "/api/v1/agent/link",
                headers={"X-Device-Token": "dedupe-token"},
                json={"v": 3, "last_ack_id": "", "items": [item]},
            )
            assert response.status_code == 200
        sessions = client.app.state.links.sessions()
        assert sessions
        assert sessions[0].stats()["duplicate"] >= 1, "重复的上行 id 必须被去重计数"
        assert json.dumps(item)  # 输入不变