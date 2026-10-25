"""下行队列的唤醒语义（0.3.1 F22 回归）。

``Outbox.pending()`` 是**未 ack 队列**：条目在 agent 确认之前一直留在里面。
载体若把"队列非空"当成"有活要干"，泵就会在每轮循环重发同一批信封，形成
热循环——CI 实录表现为 ctrl_stop 被埋在成千上万条重复消息后面，60s 内没有
投递，最终 idle 关链路（再次 DELETE 拿到 410）。这里锁死两条契约：

1. put 落在"检查 → clear"之间时必须立刻唤醒（不许睡掉唤醒）；
2. 未确认的旧信封**不算**变化（泵必须能睡，而不是热循环重发）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.unit


def _envelope(msg_id: str = "down-1"):
    from observatory.link.envelope import new_envelope

    return new_envelope("control", {"session": "s-1", "action": "stop"}, msg_id=msg_id)


def test_wait_for_change_sees_put_that_lands_before_the_wait() -> None:
    """put 落在调用方"检查 → clear"之间时不能被睡掉（F22 原始修复点）。"""

    from observatory.link.outbox import Outbox

    async def scenario() -> tuple[bool, float]:
        outbox = Outbox(maxsize=8)
        marker = outbox.revision
        outbox.put(_envelope())  # 模拟：调用方刚检查完，put 才落地
        started = time.monotonic()
        changed = await outbox.wait_for_change(5.0, since=marker)
        return changed, time.monotonic() - started

    changed, elapsed = asyncio.run(scenario())
    assert changed is True
    assert elapsed < 1.0, f"已入队的信封必须立刻唤醒泵（实际睡了 {elapsed:.2f}s）"


def test_wait_for_change_ignores_unacked_backlog() -> None:
    """已写出的未 ack 堆积不是"变化"：泵必须睡，不能热循环重发。"""

    from observatory.link.outbox import Outbox

    async def scenario() -> tuple[bool, float]:
        outbox = Outbox(maxsize=8)
        outbox.put(_envelope())  # 泵已发送，等待 agent ack
        marker = outbox.revision  # 泵已经处理过这次变化
        started = time.monotonic()
        changed = await outbox.wait_for_change(0.2, since=marker)
        return changed, time.monotonic() - started

    changed, elapsed = asyncio.run(scenario())
    assert changed is False, "未确认的旧信封不得让泵空转"
    assert elapsed >= 0.15, f"泵必须真正等待（实际 {elapsed:.2f}s）"


def test_ack_wakes_a_waiting_pump() -> None:
    """agent 的 ack 也要唤醒泵（心跳级重投以它为节拍）。"""

    from observatory.link.outbox import Outbox

    async def scenario() -> bool:
        outbox = Outbox(maxsize=8)
        outbox.put(_envelope())
        marker = outbox.revision
        waiter = asyncio.create_task(outbox.wait_for_change(5.0, since=marker))
        await asyncio.sleep(0)
        outbox.ack("down-1")
        return await waiter

    assert asyncio.run(scenario()) is True


def test_wait_for_messages_ends_the_hold_when_nothing_is_queued() -> None:
    """HTTP 载体的 hold 窗口仍然按超时结束（不能因为已消费的 ack 而空转）。"""

    from observatory.link.outbox import Outbox

    async def scenario() -> tuple[list[str], float]:
        outbox = Outbox(maxsize=8)
        outbox.put(_envelope())
        outbox.ack("down-1")
        started = time.monotonic()
        messages = await outbox.wait_for_messages(0.2)
        return [item.id for item in messages], time.monotonic() - started

    messages, elapsed = asyncio.run(scenario())
    assert messages == []
    assert 0.15 <= elapsed < 2.0, f"hold 必须等满窗口后返回（实际 {elapsed:.2f}s）"
