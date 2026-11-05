"""0.3.16 控制帧拥塞合并：保最新帧、绝不拖住链路循环。

真机事故（SKUSER-AEK5NHEC）：学校 NAT 上行 < 帧率产出时，逐帧 ``await ws.send``
让 keepalive pong 排在帧数据后 >20s，服务端 ``1011 keepalive ping timeout`` 断链。
"""

from __future__ import annotations

import asyncio

from observatory_agent.core import link as link_mod
from observatory_agent.core.link import Link


class _RecordingWS:
    def __init__(self, delay_s: float = 0.05) -> None:
        self.sent: list[bytes] = []
        self._delay = delay_s

    async def send(self, blob: bytes) -> None:
        await asyncio.sleep(self._delay)
        self.sent.append(blob)


def _make_link() -> Link:
    return Link(token="tok", base_urls=["https://example.invalid"], device_uid="OBS-T")


def test_burst_coalesces_to_latest_frame() -> None:
    """连发 10 帧不等待：写任务只发得出最新一帧（旧帧被替换丢弃）。"""

    async def scenario() -> None:
        link = _make_link()
        ws = _RecordingWS()
        link._ws = ws
        link._carrier = "ws"
        for i in range(10):
            ok = await link.send_control_frame("s1", f"frame-{i}".encode(), ts_ms=1000 + i)
            assert ok
        await asyncio.wait_for(link._ctrl_tasks["s1"], 2.0)
        assert len(ws.sent) == 1
        assert b"frame-9" in ws.sent[0]
        assert link._ctrl_dropped == 9
        # 写任务自清，槽位不留残留
        assert "s1" not in link._ctrl_tasks
        assert "s1" not in link._ctrl_pending

    asyncio.run(scenario())


def test_slow_sender_drops_stale_frames_keeps_streaming() -> None:
    """慢速发送下持续入队：只丢陈旧帧，链路调用点立即返回不被阻塞。"""

    async def scenario() -> None:
        link = _make_link()
        ws = _RecordingWS(delay_s=0.05)
        link._ws = ws
        link._carrier = "ws"
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        # 生产者以 2ms 间隔发 20 帧，但每帧发送要 50ms：中途让出给写任务
        for i in range(20):
            await link.send_control_frame("s1", f"f{i:02d}".encode(), ts_ms=i)
            if i % 3 == 0:
                await asyncio.sleep(0.005)  # 给写任务发送机会
        elapsed = loop.time() - t0
        assert elapsed < 1.5  # 20 帧若逐帧串行要 >1s；合并后调用点几乎零等待
        task = link._ctrl_tasks.get("s1")
        if task is not None:
            await asyncio.wait_for(task, 2.0)
        assert 1 <= len(ws.sent) < 20
        assert link._ctrl_dropped > 0
        # 最后一帧一定送达（画面新鲜度优先）
        assert b"f19" in ws.sent[-1]

    asyncio.run(scenario())


def test_send_timeout_drops_frame_not_link(monkeypatch) -> None:
    """发送挂起超过上限：丢这一帧并退出写任务，链路本体不受影响。"""

    monkeypatch.setattr(link_mod, "CTRL_SEND_TIMEOUT_S", 0.05)

    class _StuckWS:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        async def send(self, blob: bytes) -> None:
            self.sent.append(blob)
            await asyncio.sleep(10)  # 永远发不完

    async def scenario() -> None:
        link = _make_link()
        ws = _StuckWS()
        link._ws = ws
        link._carrier = "ws"
        ok = await link.send_control_frame("s1", b"stuck", ts_ms=1)
        assert ok
        task = link._ctrl_tasks["s1"]
        await asyncio.wait_for(task, 2.0)  # 超时后写任务自己结束，不悬挂
        assert link._ctrl_dropped == 1
        assert "s1" not in link._ctrl_tasks

    asyncio.run(scenario())


def test_no_carrier_returns_false_without_task() -> None:
    async def scenario() -> None:
        link = _make_link()
        ok = await link.send_control_frame("s1", b"x")
        assert ok is False
        assert not link._ctrl_tasks

    asyncio.run(scenario())


def test_carrier_switch_resets_senders() -> None:
    """WS 断开清理：在途帧与写任务全部作废，不留跨载体残帧。"""

    async def scenario() -> None:
        link = _make_link()
        ws = _RecordingWS(delay_s=0.05)
        link._ws = ws
        link._carrier = "ws"
        await link.send_control_frame("s1", b"a")
        await link.send_control_frame("s1", b"b")
        link._reset_control_senders()
        assert not link._ctrl_pending
        assert not link._ctrl_tasks
        await asyncio.sleep(0.2)
        # 槽位清空后写任务最多发出已进入发送的旧帧，不会复活
        assert all(blob in (b"a", b"b") or b"ctrl" in str(blob) for blob in ws.sent)

    asyncio.run(scenario())
