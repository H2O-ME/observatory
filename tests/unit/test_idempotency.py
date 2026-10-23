"""幂等与重投（spec §5.3/§5.6，A9/A10）。W1 起启用。

§5.6 的 ack 语义（已裁决）：
- 请求里的 `last_ack_id` 是**受控端最后收到的下行 id**（客户端 → 服务端）；
- 响应里的 `ack_id` 是**服务端最后确认的上行 id**（服务端 → 客户端）；
两者方向相反，不能混用；`ack_id` 绝不是"最后一条下行 id"。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.skip(reason="W1: 等待 link/outbox.py 的 last_ack 窗口（spec §5.6）")
def test_outbox_redelivery_is_bounded_and_idempotent() -> None:
    from observatory.link.outbox import Outbox  # noqa: PLC0415

    outbox = Outbox(capacity=8)
    downlink = {
        "v": 3,
        "id": "down-1",
        "t": "command",
        "ts_ms": 1_730_000_000_000,
        "body": {"command_id": "cmd-1", "type": "files", "payload": {}, "deadline_ms": 25_000},
    }
    outbox.enqueue(downlink)
    # 未收到 last_ack_id → 重投
    assert [item["id"] for item in outbox.batch(last_ack_id="")] == ["down-1"]
    assert [item["id"] for item in outbox.batch(last_ack_id="")] == ["down-1"]
    # 收到 last_ack_id=<下行 id> → 不再重发该下行
    assert outbox.batch(last_ack_id="down-1") == []


@pytest.mark.skip(reason="W1: 命令 journal 的幂等键（A10 无重复执行）")
def test_command_journal_dedupes_by_id() -> None:
    from observatory.link.journal import CommandJournal  # noqa: PLC0415

    journal = CommandJournal()
    journal.record_delivery("cmd-1", attempt=1)
    journal.record_delivery("cmd-1", attempt=2)
    assert journal.delivery_count("cmd-1") == 1


@pytest.mark.skip(reason="W1: 帧 X-Frame-Id 幂等（spec §5.6；A10）")
def test_frame_idempotency_key() -> None:
    from observatory.media.ingest import FrameIdempotency  # noqa: PLC0415

    gate = FrameIdempotency()
    assert gate.claim("frame-abc") is True
    assert gate.claim("frame-abc") is False
    assert gate.claim("frame-def") is True


@pytest.mark.skip(reason="W1: 需要 HTTP 载体 + session 的 ack_id 推进逻辑（spec §5.6）")
def test_ack_id_advances_to_last_uplink_id() -> None:
    """发 3 条上行 a,b,c → 响应 ack_id == c（ack 的是上行，不是下行）。"""

    from fastapi.testclient import TestClient

    from observatory.main import create_app
    from tests.contract.fixtures import agent_0_2_10 as legacy

    with TestClient(create_app()) as client:
        token = client.post(
            "/api/v1/agent/enroll", json=legacy.enroll_request(code="test-enroll-code")
        ).json()["token"]
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
        assert response.status_code == 200
        assert response.json()["ack_id"] == "c"