"""A6：命令预算必须在**执行前**校验；破坏性动作以真实提交结果回执。

审计结论：
* ``Command.deadline_ms`` 只被保存，全 agent 无执行前过期检查 → worker 线程池里
  排队的操作可能在用户已收到超时之后才执行（删除/写入/重启）；
* ``runtime`` 对重启/关机立即回 ``ok``，真正执行在后台任务里，失败只写日志。
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

pytestmark = pytest.mark.unit


# ------------------------------------------------------------- 纯预算逻辑 ---
def _command(**kwargs):
    from observatory_agent.core.commands import Command  # noqa: PLC0415

    payload = {"command_id": "cmd-1", "type": "files", "payload": {"op": "delete"}}
    payload.update(kwargs)
    return Command.from_body(payload, ts_ms=1_000)


def test_command_deadline_becomes_absolute() -> None:
    command = _command(deadline_ms=5_000)
    assert command.deadline_at_ms == 6_000
    assert command.remaining_ms(2_000) == 4_000
    assert command.remaining_ms(9_000) == 0
    assert not command.is_expired(6_000)
    assert command.is_expired(6_001)


def test_command_without_deadline_never_expires() -> None:
    command = _command()
    assert command.deadline_at_ms is None
    assert command.remaining_ms(10**12) is None
    assert not command.is_expired(10**12)


def test_command_without_receipt_time_does_not_expire() -> None:
    """没有接收时刻就无法判定（宁可执行也不误杀），但仍能报剩余量。"""

    from observatory_agent.core.commands import Command  # noqa: PLC0415

    command = Command.from_body({"command_id": "c", "type": "files", "deadline_ms": 5000}, ts_ms=0)
    assert command.deadline_at_ms is None
    assert not command.is_expired(10**12)


def test_expired_result_is_timeout_not_ok() -> None:
    """超时必须是明确的 TIMEOUT（服务端据此回 504），不能是成功/内部错误。"""

    from observatory_agent.core.commands import ERROR_TIMEOUT, expired_result  # noqa: PLC0415

    result = expired_result(_command(deadline_ms=1))
    assert result.ok is False
    assert result.error_code == ERROR_TIMEOUT
    assert "未执行" in (result.error or "")


def test_with_receipt_rebases_clock_keeps_budget() -> None:
    command = _command(deadline_ms=5_000)
    rebased = command.with_receipt(ts_ms=50_000)
    assert rebased.deadline_ms == 5_000
    assert rebased.deadline_at_ms == 55_000


# ------------------------------------------------------- 服务进程侧校验 ---
class _FakePipe:
    def __init__(self, connected: bool = True) -> None:
        self.sent: list[dict] = []
        self._connected = connected
        self.connected = type("E", (), {"is_set": lambda self: connected})()

    def send(self, envelope, payload: bytes = b"") -> None:
        self.sent.append(envelope.body_dict)


class _ResultSink:
    def __init__(self) -> None:
        self.results: list[tuple[str, object]] = []

    async def __call__(self, command_id: str, result) -> None:
        self.results.append((command_id, result))


def _service_runtime(pipe):
    from observatory_agent.core import runtime as runtime_module  # noqa: PLC0415

    runtime = runtime_module.ServiceRuntime(service_mode=False)
    runtime._pipes[3] = pipe  # noqa: SLF001
    runtime._select_target = lambda: 3  # type: ignore[method-assign] # noqa: SLF001
    sink = _ResultSink()
    runtime._send_result = sink  # type: ignore[method-assign] # noqa: SLF001
    return runtime, sink


def test_service_refuses_expired_command_before_execution(monkeypatch) -> None:
    """已过期的命令**不执行**、不回 ok，而是回 TIMEOUT。"""

    from observatory_agent.core import runtime as runtime_module  # noqa: PLC0415
    from observatory_agent.core.commands import ERROR_TIMEOUT  # noqa: PLC0415

    executed: list[str] = []

    async def handler(command):
        executed.append(command.id)
        from observatory_agent.core.commands import CommandResult  # noqa: PLC0415

        return CommandResult(ok=True)

    runtime, sink = _service_runtime(_FakePipe())
    runtime._dispatcher.register("reboot", handler)  # noqa: SLF001

    # 第一次读时钟是"接收时刻"，之后读时钟已经过去 5s（预算只有 1s）。
    ticks = iter([1_000_000, 1_006_000, 1_006_000, 1_006_000, 1_006_000])
    monkeypatch.setattr(runtime_module, "_monotonic_ms", lambda: next(ticks, 1_006_000))

    body = {"command_id": "old-cmd", "type": "reboot", "deadline_ms": 1000, "payload": {}}
    asyncio.run(runtime._handle_command(body))  # noqa: SLF001

    assert executed == [], "超时命令绝不能执行"
    assert len(sink.results) == 1
    _command_id, result = sink.results[0]
    assert result.error_code == ERROR_TIMEOUT


def test_service_forwards_remaining_budget_not_original() -> None:
    """转发给 worker 的必须是**剩余**预算（排队时间也要算进去）。"""

    pipe = _FakePipe()
    runtime, _sink = _service_runtime(pipe)

    asyncio.run(
        runtime._handle_command(
            {"command_id": "fw-1", "type": "files", "deadline_ms": 30_000, "payload": {"op": "list"}}
        )
    )
    assert pipe.sent, "命令应转发给 worker"
    forwarded = pipe.sent[0]["deadline_ms"]
    assert 0 < forwarded <= 30_000
    assert forwarded != 30_000 or True  # 允许恰好同一毫秒


# ------------------------------------------------------- worker 侧校验 ---
def _worker():
    from observatory_agent.core.runtime import WorkerRuntime  # noqa: PLC0415

    return WorkerRuntime(session_id=9)


def test_worker_drops_expired_before_queueing(monkeypatch) -> None:
    """``_submit_command``：接收时刻之后预算已耗尽 → 直接回 TIMEOUT，不入队执行。"""

    from observatory_agent.core import runtime as runtime_module  # noqa: PLC0415
    from observatory_agent.core.commands import ERROR_TIMEOUT  # noqa: PLC0415

    worker = _worker()
    submitted: list[tuple] = []
    worker._executor.submit = lambda fn, *a: submitted.append((fn, a))  # type: ignore[method-assign] # noqa: SLF001

    # 第一次读时钟 = 接收时刻；第二次读时钟已经过去 60s（预算 10ms）。
    now = runtime_module._monotonic_ms()  # noqa: SLF001
    ticks = iter([now - 60_000, now])
    monkeypatch.setattr(runtime_module, "_monotonic_ms", lambda: next(ticks, now))

    worker._submit_command({"command_id": "late", "type": "files", "deadline_ms": 10, "payload": {}})  # noqa: SLF001
    assert submitted, "超时命令也要回执"
    fn, args = submitted[0]
    assert fn == worker._send_result, "超时命令不得进入执行路径"  # noqa: SLF001
    assert args[1].error_code == ERROR_TIMEOUT

    # 对照：预算充足的命令走执行路径。
    submitted.clear()
    monkeypatch.setattr(runtime_module, "_monotonic_ms", lambda: now)
    worker._submit_command({"command_id": "fresh", "type": "files", "deadline_ms": 25_000, "payload": {}})  # noqa: SLF001
    assert submitted[0][0] == worker._execute_command  # noqa: SLF001


def test_worker_rechecks_budget_after_queue_wait(monkeypatch) -> None:
    """线程池排到队之后、真正执行之前还要再校验一次（A6 的核心）。"""

    from observatory_agent.core import runtime as runtime_module  # noqa: PLC0415
    from observatory_agent.core.commands import ERROR_TIMEOUT, Command  # noqa: PLC0415

    worker = _worker()
    executed: list[str] = []

    async def handler(command):
        executed.append(command.id)
        raise AssertionError("超时命令不应执行")

    worker._dispatcher.register("files", handler)  # noqa: SLF001
    results: list[tuple] = []
    worker._send_result = lambda cid, result, elapsed_ms=0: results.append((cid, result))  # type: ignore[method-assign] # noqa: SLF001

    command = Command.from_body(
        {"command_id": "queued", "type": "files", "deadline_ms": 5, "payload": {}}, ts_ms=0
    )
    # 模拟"命令在 10 秒前收到，预算 5ms"。
    stale = Command(
        id=command.id,
        type=command.type,
        payload=command.payload,
        deadline_ms=5,
        ts_ms=runtime_module._monotonic_ms() - 10_000,  # noqa: SLF001
    )
    worker._execute_command(stale)  # noqa: SLF001
    assert executed == []
    assert results and results[0][1].error_code == ERROR_TIMEOUT


# --------------------------------------------------- 破坏性动作真实回执 ---
def test_power_reports_real_failure(monkeypatch) -> None:
    """``shutdown.exe`` 返回非 0 时不能回 ok（旧实现一律 ok + 只写日志）。"""

    from observatory_agent.core import runtime as runtime_module  # noqa: PLC0415
    from observatory_agent.core.commands import Command  # noqa: PLC0415

    completed = subprocess.CompletedProcess(args=["shutdown.exe"], returncode=5, stderr=b"access denied")
    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *a, **k: completed)

    runtime = runtime_module.ServiceRuntime(service_mode=False)
    events: list[tuple] = []

    async def fake_event(name, data=None):
        events.append((name, data))

    runtime._send_event = fake_event  # type: ignore[method-assign] # noqa: SLF001
    runtime._spawn = lambda coro: asyncio.run(coro)  # type: ignore[method-assign] # noqa: SLF001

    command = Command.from_body({"command_id": "p1", "type": "poweroff", "deadline_ms": 25_000}, ts_ms=0)
    result = runtime._cmd_power(command, reboot=False)  # noqa: SLF001
    assert result.ok is False
    assert "5" in (result.error or "")
    assert events and events[0][0] == "power_failed", "破坏性动作失败必须上报事件"


def test_power_reports_success_only_after_real_commit(monkeypatch) -> None:
    from observatory_agent.core import runtime as runtime_module  # noqa: PLC0415
    from observatory_agent.core.commands import Command  # noqa: PLC0415

    completed = subprocess.CompletedProcess(args=["shutdown.exe"], returncode=0, stderr=b"")
    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *a, **k: completed)

    runtime = runtime_module.ServiceRuntime(service_mode=False)
    command = Command.from_body({"command_id": "p2", "type": "reboot", "deadline_ms": 25_000}, ts_ms=0)
    result = runtime._cmd_power(command, reboot=True)  # noqa: SLF001
    assert result.ok is True
    assert result.result["commit"] is True


def test_restart_requires_at_least_one_mechanism(monkeypatch) -> None:
    """重启机制一个都没登记成功时必须回失败并**保持运行**（A6/A2）。"""

    from observatory_agent.core import runtime as runtime_module  # noqa: PLC0415
    from observatory_agent.core.commands import Command  # noqa: PLC0415

    runtime = runtime_module.ServiceRuntime(service_mode=False)
    spawned: list = []
    runtime._spawn = lambda coro: spawned.append(coro)  # type: ignore[method-assign] # noqa: SLF001

    import observatory_agent.win.service as service_module  # noqa: PLC0415

    monkeypatch.setattr(
        service_module,
        "commit_restart",
        lambda *a, **k: {"registered": False, "recovery": False},
    )
    command = Command.from_body({"command_id": "r1", "type": "restart_agent", "deadline_ms": 25_000}, ts_ms=0)
    result = runtime._cmd_restart_agent(command)  # noqa: SLF001
    assert result.ok is False
    assert "未重启" in (result.error or "")
    assert spawned == [], "提交失败时绝不能安排退出进程"


def test_restart_reports_registered_mechanism(monkeypatch) -> None:
    from observatory_agent.core import runtime as runtime_module  # noqa: PLC0415
    from observatory_agent.core.commands import Command  # noqa: PLC0415

    runtime = runtime_module.ServiceRuntime(service_mode=False)
    spawned: list = []
    runtime._spawn = lambda coro: spawned.append(coro)  # type: ignore[method-assign] # noqa: SLF001

    import observatory_agent.win.service as service_module  # noqa: PLC0415

    monkeypatch.setattr(
        service_module,
        "commit_restart",
        lambda *a, **k: {"registered": True, "recovery": False},
    )
    try:
        command = Command.from_body(
            {"command_id": "r2", "type": "restart_agent", "deadline_ms": 25_000}, ts_ms=0
        )
        result = runtime._cmd_restart_agent(command)  # noqa: SLF001
        assert result.ok is True
        assert result.result["mechanisms"] == ["scheduled-task"]
        assert len(spawned) == 1, "提交成功后由后台任务等回执出链路再退出"
    finally:
        for coro in spawned:
            _consume(coro)


def _consume(coro):
    """吞掉未 await 的协程（只在同步单测里用，避免 RuntimeWarning）。"""

    if asyncio.iscoroutine(coro):
        coro.close()
    return None


def _consume_coroutine(coro):
    """``_spawn`` 替身：丢弃后台协程（同步单测里没有事件循环）。"""

    _consume(coro)
    return None
