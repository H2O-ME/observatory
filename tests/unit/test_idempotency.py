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


class _StepClock:
    """可手动推进的时钟：替换 journal._clock，用于跨过重投退避窗口。"""

    def __init__(self, now: int) -> None:
        self._now = now

    def now_ms(self) -> int:
        return self._now


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

        async def scenario() -> tuple[list[str], list[str], list[str]]:
            journal = client.app.state.journal
            await journal.create(device_id, "sysinfo", {"seq": 1})
            await journal.create(device_id, "sysinfo", {"seq": 2})
            first = [record.id for record in await journal.replay_for_device(device_id)]
            # 退避窗口内立即重连：节流不重发，且不改变任何 id / 状态
            second = [record.id for record in await journal.replay_for_device(device_id)]
            # 时钟越过退避窗口后：同样的稳定 id 再次投递（agent 按 command_id 去重）
            journal._clock = _StepClock(journal._clock.now_ms() + 2_000)
            third = [record.id for record in await journal.replay_for_device(device_id)]
            return first, second, third

        first, second, third = client.portal.call(scenario)
        assert len(first) == 2
        assert second == [], "退避窗口内的重复重连不应重发"
        assert first == third, "退避窗口后的重投必须复用稳定 command_id，否则 agent 无法去重"


def test_command_journal_replay_exhaustion_marks_failed(tmp_dir: Path) -> None:
    """A10：超过 MAX_REPLAY_ATTEMPTS 次真实重投后命令置 failed/REPLAY_EXHAUSTED（可观测、不再重发）。"""

    from observatory.link.journal import MAX_REPLAY_ATTEMPTS, replay_backoff_ms
    from tests.helpers import running_app, seed_device

    with running_app(tmp_dir) as client:
        device_id = seed_device(client, uid="OBS-EXHAUST", token="exhaust-token")

        async def scenario() -> tuple[int, str, str]:
            journal = client.app.state.journal
            record = await journal.create(device_id, "sysinfo", {"seq": 1})
            delivered: list[str] = []
            attempts = 0
            # 每轮把时钟推过当前退避窗口，保证每轮都是一次"真实"重投；
            # 累计 ~20s，远小于 create 的 ≥60s 过期窗口，不会误触过期过滤。
            for _ in range(MAX_REPLAY_ATTEMPTS + 2):
                journal._clock = _StepClock(journal._clock.now_ms() + replay_backoff_ms(attempts) + 1_000)
                batch = [item.id for item in await journal.replay_for_device(device_id)]
                if not batch:
                    break
                delivered.extend(batch)
                attempts += 1
            final = await journal.get(record.id)
            assert final is not None
            return len(delivered), final.status, final.error_code or ""

        count, status, error_code = client.portal.call(scenario)
        assert count == MAX_REPLAY_ATTEMPTS, "恰好重投 MAX_REPLAY_ATTEMPTS 次"
        assert status == "failed"
        assert error_code == "REPLAY_EXHAUSTED"


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