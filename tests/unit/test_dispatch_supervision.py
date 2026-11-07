"""0.3.22：管道写护栏 + 分发循环监督（真机事故 2026-11-07 回归）。

事故：四楼公共教室（poll 载具）下行命令全灭一整天——心跳/截帧正常、命令
attempts=0。0.3.21 只护栏了 `_forward_to_worker` 的管道写；`_broadcast_config`
/`_send_config_to`/`_forward_control` 仍是裸阻塞 `pipe.send`，worker 挂死时
阻塞 `WriteFile` 卡死分发循环，设备"在线但永聋"。本文件钉死三件事：

1. 所有服务进程 → worker 的管道写都过 `_pipe_send` 护栏：超时 + force_close
   解救阻塞线程（否则每次超时泄漏一个执行器线程，直到 to_thread 全面瘫痪）；
2. 分发循环停滞看门狗：超过 `DISPATCH_STALL_WATCHDOG_S` 没消费下行就重生；
3. 内部派发（welcome 的 update_offer）与命令路径同款看门狗，不再裸 await。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from observatory_agent.core import runtime as runtime_module
from observatory_agent.core.commands import Command

pytestmark = pytest.mark.unit


class _StuckPipe:
    """send 永久阻塞的管道替身：模拟 worker 挂死不读管道。"""

    def __init__(self) -> None:
        self.connected = threading.Event()
        self.connected.set()
        self._release = threading.Event()
        self.force_closed = False
        self.send_calls = 0

    def send(self, message: Any, payload: bytes = b"") -> None:
        self.send_calls += 1
        self._release.wait(timeout=10)

    def force_close(self) -> None:
        self.force_closed = True
        self._release.set()

    def release(self) -> None:
        self._release.set()


def _runtime() -> Any:
    return runtime_module.ServiceRuntime(service_mode=False)


@pytest.mark.asyncio
async def test_pipe_send_timeout_forces_close_and_raises() -> None:
    """护栏超时必须 force_close（解救阻塞线程）并向调用方抛 TimeoutError。"""

    runtime = _runtime()
    pipe = _StuckPipe()
    with pytest.raises(asyncio.TimeoutError):
        await runtime._pipe_send(1, pipe, {"t": "config"}, timeout_s=0.2)
    assert pipe.force_closed is True
    pipe.release()


@pytest.mark.asyncio
async def test_broadcast_config_survives_stuck_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 挂死时配置广播必须在护栏超时内返回，绝不冻结事件循环（事故主路径）。"""

    monkeypatch.setattr(runtime_module, "PIPE_SEND_TIMEOUT_S", 0.2)
    runtime = _runtime()
    pipe = _StuckPipe()
    runtime._pipes[1] = pipe
    started = time.monotonic()
    await runtime._broadcast_config()  # 旧实现：这里会永久卡死
    assert time.monotonic() - started < 5.0
    assert pipe.force_closed is True
    pipe.release()


@pytest.mark.asyncio
async def test_send_config_to_survives_stuck_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """hello 后的配置下发同样走护栏（事故同类后门）。"""

    monkeypatch.setattr(runtime_module, "PIPE_SEND_TIMEOUT_S", 0.2)
    runtime = _runtime()
    pipe = _StuckPipe()
    runtime._pipes[7] = pipe
    started = time.monotonic()
    await runtime._send_config_to(7)
    assert time.monotonic() - started < 5.0
    assert pipe.force_closed is True
    pipe.release()


@pytest.mark.asyncio
async def test_forward_control_survives_stuck_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """控制输入转发走护栏：超时回 control_failed 事件而不是卡死分发循环。"""

    monkeypatch.setattr(runtime_module, "PIPE_SEND_TIMEOUT_S", 0.2)
    runtime = _runtime()
    pipe = _StuckPipe()
    runtime._pipes[1] = pipe
    events: list[tuple[str, dict[str, Any]]] = []

    async def fake_send_event(name: str, body: dict[str, Any]) -> None:
        events.append((name, body))

    monkeypatch.setattr(runtime, "_send_event", fake_send_event)
    started = time.monotonic()
    # action="start" 建租约并路由到 pipe（session 1 是唯一已连接会话）
    await runtime._forward_control({"session": "tok", "action": "start", "ev": {}})
    assert time.monotonic() - started < 5.0
    assert events and events[0][0] == "control_failed"
    pipe.release()


class _FakeLink:
    """只会"连接着但收不到下行"的链路替身。"""

    def __init__(self) -> None:
        self.connected_flag = True
        self.received: list[Any] = []

    def connected(self) -> bool:
        return self.connected_flag

    async def recv(self) -> Any:
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_supervisor_respawns_stalled_dispatch_loop() -> None:
    """分发循环停滞超阈值 → 监督器取消旧任务并重生（事故场景的自愈路径）。"""

    runtime = _runtime()
    runtime._link = _FakeLink()
    # 模拟循环 1 小时没消费任何下行
    runtime._dispatch_alive_at = time.monotonic() - 3600.0
    runtime._supervise_dispatch_loop()
    assert runtime._dispatch_respawn_count == 1
    task = runtime._dispatch_task
    assert task is not None and not task.done()
    task.cancel()


@pytest.mark.asyncio
async def test_supervisor_ignores_fresh_loop_and_offline_link() -> None:
    """刚消费过 / 链路离线：不重生（离线时 recv 收不到东西不算循环的错）。"""

    runtime = _runtime()
    runtime._link = _FakeLink()
    runtime._dispatch_alive_at = time.monotonic()
    runtime._supervise_dispatch_loop()  # 刚消费过：不重生
    assert runtime._dispatch_respawn_count == 0

    runtime._link.connected_flag = False
    runtime._dispatch_alive_at = time.monotonic() - 3600.0
    runtime._supervise_dispatch_loop()
    assert runtime._dispatch_respawn_count == 0
    # 离线时时间戳被复位，链路恢复后从零起算
    assert time.monotonic() - runtime._dispatch_alive_at < 1.0


@pytest.mark.asyncio
async def test_internal_dispatch_watchdog_caps_hung_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """welcome 的 update_offer 走同款看门狗：处理器挂死不再永久占住分发循环。"""

    monkeypatch.setattr(runtime_module, "COMMAND_HANDLER_WATCHDOG_S", 0.2)
    runtime = _runtime()

    async def hang(command: Command) -> Any:
        await asyncio.sleep(3600)

    runtime._dispatcher.register("update", hang)
    started = time.monotonic()
    result = await runtime._dispatch_internal("update", {"version": "9.9.9"})
    assert time.monotonic() - started < 5.0
    assert result.ok is False and result.error_code == "TIMEOUT"
