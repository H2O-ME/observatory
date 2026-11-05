"""A3：控制授权必须有本地 TTL 租约、严格 session 匹配、统一按键释放。

审计结论（交叉确认过的真机现象）：
* 服务端下发 ``ttl_s``，受控端转发时丢弃 → 本地没有到期机制，丢一条 stop 就无限期控制；
* 输入不校验当前 session → 任意 stop 会停掉别人的流；
* ``tasks/input.py`` 没有"已按下键"状态，stop/断链都不释放 → 实测卡键（Ctrl、鼠标左键）。
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


# ------------------------------------------------------------- 输入账本 ---
def test_input_state_tracks_pressed_keys() -> None:
    from observatory_agent.tasks.input import InputState  # noqa: PLC0415

    state = InputState()
    state.key_down(0x11)  # CTRL
    state.key_down(0x41)  # A
    assert state.snapshot() == ((0x11, 0x41), ())
    state.key_up(0x41)
    assert state.snapshot() == ((0x11,), ())


def test_input_state_tracks_mouse_buttons() -> None:
    from observatory_agent.tasks.input import InputState  # noqa: PLC0415

    state = InputState()
    state.button_down(1)
    state.button_down(2)
    state.button_up(1)
    assert state.snapshot() == ((), (2,))


def test_release_all_emits_keyup_and_mouseup() -> None:
    """``release_all`` 必须对每个按住的键发 KEYUP、每个按钮发对应 UP 标志。"""

    from observatory_agent.tasks import input as input_module  # noqa: PLC0415

    state = input_module.InputState()
    state.key_down(0x11)
    state.key_down(0x0D)
    state.button_down(1)
    state.button_down(3)

    sent: list[tuple] = []

    def sender(events):
        sent.append(events)
        return True

    released = state.release_all(sender=sender)
    assert released == 4
    assert state.snapshot() == ((), ()), "成功释放后账本必须清空"

    # 两个键 + 两个按钮 = 4 次单事件注入
    assert len(sent) == 4
    key_flags = [events[0].union.ki.dwFlags for events in sent[:2]]
    for flags in key_flags:
        assert flags & input_module.KEYEVENTF_KEYUP
    mouse_flags = [events[0].union.mi.dwFlags for events in sent[2:]]
    assert mouse_flags[0] == input_module.MOUSEEVENTF_LEFTUP
    assert mouse_flags[1] == input_module.MOUSEEVENTF_MIDDLEUP


def test_release_all_keeps_failed_releases_for_retry() -> None:
    """释放失败时**保留**账本：宁可多抬一次，也不能漏掉一个还按着的键。"""

    from observatory_agent.tasks.input import InputState  # noqa: PLC0415

    state = InputState()
    state.key_down(0x11)
    assert state.release_all(sender=lambda events: False) == 0
    assert state.snapshot() == ((0x11,), ()), "失败的释放必须留待下次重试"
    assert state.release_all(sender=lambda events: True) == 1
    assert state.snapshot() == ((), ())


def test_handle_event_records_and_releases() -> None:
    """``handle_event`` 走真实注入路径时也要记账（用 test double 替换注入函数）。"""

    from observatory_agent.tasks import input as input_module  # noqa: PLC0415

    state = input_module.InputState()
    original = input_module.key_event
    try:
        input_module.key_event = lambda vk, down: True  # type: ignore[assignment]
        assert input_module.handle_event({"kind": "key", "code": 0x11, "down": True}, state=state)
        assert state.snapshot() == ((0x11,), ())
        assert input_module.handle_event({"kind": "key", "code": 0x11, "down": False}, state=state)
        assert state.snapshot() == ((), ())
    finally:
        input_module.key_event = original  # type: ignore[assignment]


# ------------------------------------------------------- 服务端控制租约 ---
class _FakePipe:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def connected(self):  # pragma: no cover - 只是满足 API
        raise AssertionError("not used")

    def send(self, envelope, payload: bytes = b"") -> None:
        self.sent.append(envelope.body_dict)


def _service_runtime_with_pipe(pipe):
    from observatory_agent.core import runtime as runtime_module  # noqa: PLC0415

    runtime = runtime_module.ServiceRuntime(service_mode=False)
    runtime._pipes[3] = pipe  # noqa: SLF001 - 直接构造最小现场
    runtime._pipes[3].connected = type("E", (), {"is_set": lambda self: True})()  # noqa: SLF001
    runtime._select_target = lambda: 3  # type: ignore[method-assign] # noqa: SLF001
    return runtime


def test_control_start_stores_lease_with_ttl() -> None:
    pipe = _FakePipe()
    runtime = _service_runtime_with_pipe(pipe)

    asyncio.run(runtime._forward_control({"session": "tok-1", "action": "start", "fps": 8, "ttl_s": 45}))

    lease = runtime._controls["tok-1"]  # noqa: SLF001
    # 45s 的 TTL 必须真的变成租约（旧实现直接把它丢掉了）。
    remaining = lease.remaining_ms(asyncio.get_event_loop().time() if False else runtime._control_clock())  # noqa: SLF001
    assert 40_000 <= remaining <= 45_000
    assert pipe.sent[-1]["action"] == "start"
    assert pipe.sent[-1]["lease_ms"] > 0, "worker 需要剩余量来执行自己的到期检查"


def test_control_input_from_other_session_is_dropped() -> None:
    """A3：非当前 session 的输入必须丢弃，且不影响现有流。"""

    pipe = _FakePipe()
    runtime = _service_runtime_with_pipe(pipe)
    asyncio.run(runtime._forward_control({"session": "tok-1", "action": "start", "fps": 8, "ttl_s": 60}))
    before = len(pipe.sent)

    asyncio.run(
        runtime._forward_control({"session": "tok-OTHER", "action": "input", "ev": {"kind": "key", "code": 65}})
    )
    assert len(pipe.sent) == before, "外来输入不得转发给 worker"
    assert "tok-1" in runtime._controls  # noqa: SLF001


def test_control_stop_from_other_session_does_not_end_current() -> None:
    """A3：旧实现对**任意** stop 都停流；现在只停匹配的那个会话。"""

    pipe = _FakePipe()
    runtime = _service_runtime_with_pipe(pipe)
    asyncio.run(runtime._forward_control({"session": "tok-1", "action": "start", "fps": 8, "ttl_s": 60}))

    asyncio.run(runtime._forward_control({"session": "tok-OTHER", "action": "stop"}))
    assert "tok-1" in runtime._controls  # noqa: SLF001 - 别人的 stop 不能清我们的租约

    asyncio.run(runtime._forward_control({"session": "tok-1", "action": "stop"}))
    assert "tok-1" not in runtime._controls  # noqa: SLF001


def test_control_lease_expires_locally_without_stop() -> None:
    """本地 TTL：即使一条 stop 都没收到，租约也必须到期被收割。"""

    from observatory_agent.core import runtime as runtime_module  # noqa: PLC0415

    pipe = _FakePipe()
    runtime = _service_runtime_with_pipe(pipe)
    now = runtime._control_clock()  # noqa: SLF001
    runtime._controls["tok-ttl"] = runtime_module.ControlLease(  # noqa: SLF001
        token="tok-ttl", session_id=3, expires_at=now - 0.5
    )
    expired = runtime._expire_control_leases(now)  # noqa: SLF001
    assert expired == ["tok-ttl"]
    assert runtime._controls == {}  # noqa: SLF001


def test_control_lease_uses_monotonic_not_wall_clock() -> None:
    """租约时钟必须是 loop.time()（单调），墙钟跳变不能延长/掐断控制。"""

    import time as time_module  # noqa: PLC0415

    pipe = _FakePipe()
    runtime = _service_runtime_with_pipe(pipe)
    asyncio.run(runtime._forward_control({"session": "tok-mono", "action": "start", "ttl_s": 60}))
    lease = runtime._controls["tok-mono"]  # noqa: SLF001
    assert lease.expires_at < time_module.time() + 10, "不能用墙钟秒数当截止时刻"
    assert lease.expires_at >= time_module.monotonic(), "应当基于单调时钟"


def test_lease_seconds_clamps_extremes() -> None:
    from observatory_agent.core.runtime import (  # noqa: PLC0415
        CONTROL_TTL_DEFAULT_S,
        CONTROL_TTL_MAX_S,
        CONTROL_TTL_MIN_S,
        ServiceRuntime,
    )

    assert ServiceRuntime._lease_seconds(0) == CONTROL_TTL_DEFAULT_S  # noqa: SLF001
    assert ServiceRuntime._lease_seconds("garbage") == CONTROL_TTL_DEFAULT_S  # noqa: SLF001
    assert ServiceRuntime._lease_seconds(1) == CONTROL_TTL_MIN_S  # noqa: SLF001
    assert ServiceRuntime._lease_seconds(10**9) == CONTROL_TTL_MAX_S  # noqa: SLF001
    assert ServiceRuntime._lease_seconds(600) == 600  # noqa: SLF001


def test_control_pins_session_for_lease_lifetime() -> None:
    """租约建立后输入必须钉在同一会话：会话表变化不能把画面与输入拆到两个桌面。"""

    pipe_a = _FakePipe()
    runtime = _service_runtime_with_pipe(pipe_a)
    asyncio.run(runtime._forward_control({"session": "tok-a", "action": "start", "fps": 8, "ttl_s": 60}))
    assert runtime._controls["tok-a"].session_id == 3  # noqa: SLF001

    # 模拟会话表变化：目标会话改成 5，且 5 也有管道。
    pipe_b = _FakePipe()
    runtime._pipes[5] = pipe_b  # noqa: SLF001
    pipe_b.connected = type("E", (), {"is_set": lambda self: True})()
    runtime._select_target = lambda: 5  # type: ignore[method-assign] # noqa: SLF001

    sent_before = len(pipe_a.sent)
    asyncio.run(runtime._forward_control({"session": "tok-a", "action": "input", "ev": {"kind": "key", "code": 65}}))
    assert len(pipe_a.sent) == sent_before + 1, "输入必须继续发往租约钉住的会话 3"
    assert pipe_b.sent == [], "不能漂移到新目标会话"


# ------------------------------------------------------- worker 控制清理 ---
class _FakeEngine:
    def __init__(self) -> None:
        self.control_active = None

    def set_control_active(self, active: bool) -> None:
        self.control_active = active

    def capture_control_frame(self, *, session_id: int, quality: int):  # pragma: no cover
        raise AssertionError("测试里不应真的截图")


def _worker():
    from observatory_agent.core.runtime import WorkerRuntime  # noqa: PLC0415

    worker = WorkerRuntime(session_id=7)
    worker._engine = _FakeEngine()  # noqa: SLF001
    return worker


def test_worker_cleanup_releases_pressed_keys() -> None:
    """stop / TTL / 断链统一走 ``_cleanup_control``，它必须抬起所有按键。"""

    worker = _worker()
    released: list[int] = []

    def fake_release() -> int:
        released.append(1)
        return 3

    worker._input_state.release_all = fake_release  # type: ignore[method-assign] # noqa: SLF001
    worker._control_session = "tok"  # noqa: SLF001
    worker._cleanup_control(reason="stop:stop", notify=False)  # noqa: SLF001
    assert released == [1]
    assert worker._control_session == ""  # noqa: SLF001
    assert worker._engine.control_active is False  # type: ignore[union-attr]


def test_worker_rejects_input_from_stale_session() -> None:
    worker = _worker()
    worker._control_session = "tok-current"  # noqa: SLF001
    calls: list[dict] = []
    original = worker._input_state  # noqa: SLF001

    class _Spy:
        def release_all(self) -> int:
            calls.append({"release": True})
            return 0

    worker._input_state = _Spy()  # type: ignore[assignment] # noqa: SLF001
    try:
        # 过期会话的输入：不应注入、不应影响现有流。
        worker._handle_control({"action": "input", "session": "tok-stale", "ev": {"kind": "key", "code": 65}})
        assert calls == []
        assert worker._control_session == "tok-current"  # noqa: SLF001
    finally:
        worker._input_state = original  # type: ignore[assignment] # noqa: SLF001


def test_worker_stop_from_stale_session_keeps_current_stream() -> None:
    worker = _worker()
    worker._control_session = "tok-current"  # noqa: SLF001
    worker._handle_control({"action": "stop", "session": "tok-stale"})
    assert worker._control_session == "tok-current"  # noqa: SLF001


def test_worker_stop_without_session_keeps_current_stream() -> None:
    """畸形 stop（没有 session）也不能停掉活跃流；租约到期会兜底。"""

    worker = _worker()
    worker._control_session = "tok-current"  # noqa: SLF001
    worker._handle_control({"action": "stop"})
    assert worker._control_session == "tok-current"  # noqa: SLF001


def test_worker_lease_expiry_stops_and_releases() -> None:
    """worker 自己也要执行租约到期（服务端 stop 丢了也能自清）。"""

    import time as time_module  # noqa: PLC0415

    worker = _worker()
    worker._control_session = "tok-expired"  # noqa: SLF001
    worker._control_deadline = time_module.monotonic() - 1  # noqa: SLF001
    released: list[int] = []
    worker._input_state.release_all = lambda: released.append(1) or 2  # type: ignore[method-assign,func-returns-value] # noqa: SLF001
    worker._handle_control({"action": "input", "session": "tok-expired", "ev": {"kind": "key", "code": 65}})
    assert released == [1]
    assert worker._control_session == ""  # noqa: SLF001


def test_worker_shutdown_releases_inputs() -> None:
    worker = _worker()
    worker._control_session = "tok"  # noqa: SLF001
    released: list[int] = []
    worker._input_state.release_all = lambda: released.append(1) or 0  # type: ignore[method-assign,func-returns-value] # noqa: SLF001
    worker._shutdown()  # noqa: SLF001
    assert released == [1], "worker 退出（管道断开/注销）也必须释放按键"
