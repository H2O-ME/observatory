"""0.3.22：服务端下行投递停滞看门狗。

事故背景（2026-11-05 / 2026-11-07）：会话"在线但下行投不动"——僵尸 WS 会话
（TCP 靠 ping/pong 保活、应用层永不读）或设备消费停摆，命令在 outbox 里烂掉、
attempts=0 直接过期，服务端全程无感知。看门狗判据：outbox 最老未确认下行的
服务端 ts_ms 距今超过 ``downlink_stall_close_s`` → close(4000) 断会话逼重连、
新会话 replay_pending 重投。本文件钉死：Outbox 时间戳查询、清扫函数的
关闭/跳过/禁用三条路径。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from observatory.link.envelope import Envelope
from observatory.link.outbox import Outbox
from observatory.main import _close_stalled_downlinks

import pytest

pytestmark = pytest.mark.unit


def _envelope(ts_ms: int, msg_id: str = "m1") -> Envelope:
    return Envelope(v=3, id=msg_id, t="command", ts_ms=ts_ms, body={"command_id": msg_id})


def test_oldest_pending_ts_ms_picks_minimum_and_ignores_zero() -> None:
    outbox = Outbox()
    assert outbox.oldest_pending_ts_ms() is None  # 空队列
    outbox.put(_envelope(5_000, "a"))
    outbox.put(_envelope(3_000, "b"))
    outbox.put(_envelope(9_000, "c"))
    assert outbox.oldest_pending_ts_ms() == 3_000
    outbox.ack("a")
    outbox.ack("b")
    assert outbox.oldest_pending_ts_ms() == 9_000
    outbox.ack("c")
    assert outbox.oldest_pending_ts_ms() is None


def test_oldest_pending_ts_ms_zero_timestamps_are_not_infinitely_old() -> None:
    """ts_ms<=0 的信封不可信：整体视为无样本，绝不能判成"无穷老"触发误断。"""

    outbox = Outbox()
    outbox.put(_envelope(0, "bad"))
    assert outbox.oldest_pending_ts_ms() is None


def _fake_app(sessions: list[Any], *, threshold_s: int = 900) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(downlink_stall_close_s=threshold_s),
            links=SimpleNamespace(sessions=lambda: sessions),
        )
    )


def _fake_session(ts_ms: int | None, *, closed: bool = False) -> Any:
    outbox = Outbox()
    closed_flag = {"v": closed}

    if ts_ms is not None:
        outbox.put(_envelope(ts_ms, "stale"))

    async def close(code: int, reason: str) -> None:
        closed_flag["v"] = True
        closed_flag["code"] = code

    return SimpleNamespace(
        outbox=outbox,
        device_id=5,
        uid="OBS-TEST",
        carrier="ws",
        close=close,
        _closed_flag=closed_flag,
    )


@pytest.mark.asyncio
async def test_stale_outbox_session_is_closed() -> None:
    """最老未确认下行超过阈值 → 会话被 close(4000)。"""

    import time

    session = _fake_session(int(time.time() * 1000) - 1800_000)  # 30 分钟前
    closed = await _close_stalled_downlinks(_fake_app([session]))
    assert closed == 1
    assert session._closed_flag["v"] is True
    assert session._closed_flag["code"] == 4000


@pytest.mark.asyncio
async def test_fresh_outbox_and_empty_outbox_are_kept() -> None:
    """新鲜下行 / 空队列 / 已关闭会话：一律不动。"""

    import time

    fresh = _fake_session(int(time.time() * 1000) - 10_000)  # 10s 前
    empty = _fake_session(None)
    already_closed = _fake_session(int(time.time() * 1000) - 1800_000)
    already_closed.outbox.close()  # 真实语义：session.close() 会关掉 outbox
    closed = await _close_stalled_downlinks(_fake_app([fresh, empty, already_closed]))
    assert closed == 0
    assert all(not s._closed_flag["v"] for s in (fresh, empty, already_closed))


@pytest.mark.asyncio
async def test_zero_threshold_disables_watchdog() -> None:
    """downlink_stall_close_s=0 = 显式关闭，再老也不断。"""

    import time

    session = _fake_session(int(time.time() * 1000) - 86_400_000)
    closed = await _close_stalled_downlinks(_fake_app([session], threshold_s=0))
    assert closed == 0
    assert session._closed_flag["v"] is False
