"""幂等与重投（spec §5.3/§5.6，A9/A10）。

§5.6 的 ack 语义（已裁决）：
- 请求里的 `last_ack_id` 是**受控端最后收到的下行 id**（客户端 → 服务端）；
- 响应里的 `ack_id` 是**服务端最后确认的上行 id**（服务端 → 客户端）；
两者方向相反，不能混用；`ack_id` 绝不是"最后一条下行 id"。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_outbox_redelivery_is_bounded_and_idempotent() -> None:
    """未 ack 的下行会重投；ack 后不再出现；同一 id 入队只保留一份。"""

    from observatory.link.envelope import new_envelope
    from observatory.link.outbox import Outbox

    outbox = Outbox(maxsize=8)
    envelope = new_envelope("command", {"command_id": "cmd-1"}, msg_id="down-1")
    outbox.put(envelope)
    outbox.put(envelope)  # 同一 id 重复入队 → 幂等
    assert [item.id for item in outbox.pending()] == ["down-1"]
    assert [item.id for item in outbox.pending()] == ["down-1"], "未 ack 前必须可重投"
    assert outbox.ack("down-1") is True
    assert outbox.pending() == []
    assert outbox.last_ack_id == "down-1"


def test_command_journal_replay_uses_stable_ids(tmp_dir: Path) -> None:
    """A10：重连补投必须复用稳定 command_id（at-least-once，由 agent 去重）。"""

    from tests.helpers import running_app, seed_device

    with running_app(tmp_dir) as client:
        device_id = seed_device(client, uid="OBS-REPLAY", token="replay-token")

        async def scenario() -> tuple[list[str], list[str]]:
            journal = client.app.state.journal
            await journal.create(device_id, "sysinfo", {"seq": 1})
            await journal.create(device_id, "sysinfo", {"seq": 2})
            first = [record.id for record in await journal.replay_for_device(device_id)]
            second = [record.id for record in await journal.replay_for_device(device_id)]
            return first, second

        first, second = client.portal.call(scenario)
        assert len(first) == 2
        assert first == second, "两次重投的 command_id 必须一致，否则 agent 无法去重"


def test_frame_idempotency_key(tmp_dir: Path) -> None:
    """A10：同一 X-Frame-Id 重复上传返回同一帧、只入库一次（不重复转码）。"""

    from PIL import Image
    from sqlalchemy import func, select

    from observatory.core.models import Frame
    from tests.helpers import running_app, seed_device

    buffer = BytesIO()
    Image.new("RGB", (8, 8), color=(9, 8, 7)).save(buffer, format="WEBP")
    content = buffer.getvalue()
    params = {"device_ts_ms": 1_760_000_000_000, "session_id": 1, "kind": "screen"}
    headers = {"X-Device-Token": "frame-token", "X-Frame-Id": "frame-abc", "Content-Type": "image/webp"}

    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-FRAME", token="frame-token")
        first = client.post("/api/v1/agent/frames", params=params, headers=headers, content=content)
        second = client.post("/api/v1/agent/frames", params=params, headers=headers, content=content)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        first_body, second_body = first.json(), second.json()
        assert first_body["frame_id"] == second_body["frame_id"]
        assert first_body["ts_ms"] == second_body["ts_ms"]
        assert second_body["device_time_ms"] == 1_760_000_000_000

        async def frame_count() -> int:
            async with client.app.state.db.sessionmaker() as session:
                return int((await session.execute(select(func.count()).select_from(Frame))).scalar_one())

        assert client.portal.call(frame_count) == 1, "重复 X-Frame-Id 不能重复入库"


def test_ack_id_advances_to_last_uplink_id(tmp_dir: Path) -> None:
    """发 3 条上行 a,b,c → 响应 ack_id == c（ack 的是上行，不是下行）。"""

    from tests.helpers import running_app, seed_device

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
    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-ACK", token="ack-token")
        response = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": "ack-token"},
            json={"v": 3, "last_ack_id": "", "items": items},
        )
        assert response.status_code == 200, response.text
        assert response.json()["ack_id"] == "c"


def test_last_ack_id_suppresses_downlink_redelivery(tmp_dir: Path) -> None:
    """收到 last_ack_id 后不再重发该下行（welcome/command 都适用）。"""

    from tests.helpers import running_app, seed_device

    hello = {
        "v": 3,
        "id": "hello-ack-1",
        "t": "hello",
        "ts_ms": 1_730_000_000_000,
        "body": {
            "agent_version": "0.3.0",
            "protocol": 3,
            "boot_id": "boot-1",
            "device_uid": "OBS-ACK2",
            "last_ack_id": "",
            "device_time_ms": 1_730_000_000_000,
        },
    }
    heartbeat = {
        "v": 3,
        "id": "heartbeat-ack-1",
        "t": "heartbeat",
        "ts_ms": 1_730_000_000_100,
        "body": {"agent_version": "0.3.0", "boot_id": "boot-1", "state": {"transport": "http"}},
    }
    with running_app(tmp_dir) as client:
        seed_device(client, uid="OBS-ACK2", token="ack2-token")
        first = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": "ack2-token"},
            json={"v": 3, "last_ack_id": "", "items": [hello]},
        )
        assert first.status_code == 200
        body = first.json()
        welcome_id = body["items"][0]["id"]

        second = client.post(
            "/api/v1/agent/link",
            headers={"X-Device-Token": "ack2-token"},
            json={"v": 3, "last_ack_id": welcome_id, "items": [heartbeat]},
        )
        assert second.status_code == 200
        second_body = second.json()
        assert all(item["id"] != welcome_id for item in second_body["items"]), (
            "已 ack 的下行不能重发（§5.6 last_ack_id 抑制重投）"
        )
        assert second_body["ack_id"] == "heartbeat-ack-1"