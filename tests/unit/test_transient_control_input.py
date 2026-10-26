"""实时控制输入不得占用可靠下行队列（2026-10-27 生产反馈回归）。

现象：开一会儿实时控制后，文件操作（`drives`）报
“受控端内部错误：下行队列已满，请稍后重试”，且命令从未下发。

根因：每个输入事件都走 ``AgentSession.send`` 进 256 条的有界 outbox，而 WS
载体上受控端只在 hello 里报一次下行游标——非命令信封永远不会被 ack。30 Hz
的鼠标移动几秒就把队列灌满，之后任何命令都在入队时报错。

这里锁死两条契约：
1. ``send_transient`` 走载体的直发口，一条都不进 outbox；
2. 任意上行信封（WS 用 heartbeat/pong 回带 ``last_ack_id``）都能推进下行游标。
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


def _session(maxsize: int = 256, sender=None):
    from observatory.core.config import Settings
    from observatory.link.session import AgentSession

    session = AgentSession(
        device_id=1,
        uid="OBS-TRANSIENT",
        journal=None,  # type: ignore[arg-type] - 本用例不触 journal
        settings=Settings(outbox_max_messages=maxsize),
    )
    if sender is not None:
        session.set_direct_sender(sender)
    return session


def _input_body() -> dict[str, object]:
    return {"session": "tok", "action": "input", "ev": {"kind": "mouse", "action": "move", "x": 1, "y": 2}}


def test_transient_input_bypasses_the_bounded_outbox() -> None:
    """输入洪峰不占 outbox：队列上限 16 也塞 200 条不会满。"""

    from observatory.link.envelope import Envelope

    delivered: list[Envelope] = []

    async def sender(envelope: Envelope) -> bool:
        delivered.append(envelope)
        return True

    session = _session(maxsize=16, sender=sender)

    async def scenario() -> None:
        for _ in range(200):
            assert await session.send_transient("control", _input_body()) is True
        assert session.outbox.pending_count() == 0, "输入事件不得进入可靠队列"
        # 可靠通道仍然可用：修复前这里会抛“下行队列已满，请稍后重试”。
        await session.send(
            "command",
            {"command_id": "cmd-after-flood", "type": "files", "payload": {"op": "drives"}},
            deadline_ms=25_000,
        )
        assert session.outbox.pending_count() == 1

    asyncio.run(scenario())
    assert len(delivered) == 200


def test_transient_without_carrier_is_dropped_not_queued() -> None:
    """没有直发口（例如 HTTP 载体）时直接丢，绝不回落到 outbox。"""

    session = _session(maxsize=16)

    async def scenario() -> tuple[bool, int]:
        ok = await session.send_transient("control", _input_body())
        return ok, session.outbox.pending_count()

    ok, pending = asyncio.run(scenario())
    assert ok is False
    assert pending == 0


def test_uplink_cursor_acks_downlink_over_ws() -> None:
    """heartbeat / pong 里的 last_ack_id 必须清掉服务端 outbox。"""

    from observatory.link.envelope import new_envelope

    session = _session()

    async def scenario() -> tuple[int, int]:
        await session.send(
            "control",
            {"session": "tok", "action": "start", "fps": 8, "quality": 60},
            deadline_ms=10_000,
        )
        assert session.outbox.pending_count() == 1
        heartbeat = new_envelope(
            "heartbeat",
            {
                "agent_version": "0.3.1",
                "boot_id": "boot",
                "last_ack_id": session.outbox.pending()[0].id,
                "device_time_ms": 1_730_000_000_000,
            },
            msg_id="hb-ack",
        )
        await session.handle_uplink(heartbeat)
        after_heartbeat = session.outbox.pending_count()

        await session.send(
            "control",
            {"session": "tok", "action": "stop"},
            deadline_ms=10_000,
        )
        pong = new_envelope(
            "pong",
            {
                "device_time_ms": 1_730_000_000_000,
                "last_ack_id": session.outbox.pending()[0].id,
            },
            msg_id="pong-ack",
        )
        await session.handle_uplink(pong)
        return after_heartbeat, session.outbox.pending_count()

    after_heartbeat, after_pong = asyncio.run(scenario())
    assert after_heartbeat == 0, "heartbeat 的 last_ack_id 必须推进下行游标"
    assert after_pong == 0, "pong 的 last_ack_id 必须推进下行游标"
