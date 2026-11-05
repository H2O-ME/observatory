"""0.3.18/0.3.21：``exec`` 命令处理器（SYSTEM 身份、服务进程执行）。

关键契约：
- ``exec_enabled`` 关闭（或缺省）时**绝不执行**，回 UNSUPPORTED——这是任意命令
  执行功能的安全总闸，必须有测试钉死"默认关"。
- 子进程超时回 TIMEOUT 且带回已捕获的部分输出（排障需要）。
- **0.3.21 超时杀树**：``subprocess.run(timeout)`` 超时只杀直接子进程，常驻
  循环的孙进程握住管道会让收尾读取永久挂死（真机事故：分发循环冻结 1.5h），
  现在超时必须 ``taskkill /T /F`` 杀整棵树。测试用假 Popen 验证调用序列。
- stdout/stderr 截断带标志（回执要走 1MiB 信封）。
- ``popen`` 参数注入替身，测试绝不真跑 shell（除最后一个真 echo 用例）。
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest

from observatory_agent.core.commands import Command
from observatory_agent.tasks.exec import (
    clamp_timeout_s,
    make_exec_handler,
    run_command,
    truncate,
)

pytestmark = pytest.mark.unit


class _FakeProcess:
    """Popen 替身：可编排"先超时后返回"与正常返回。"""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        timeout_first: bool = False,
        pid: int = 4242,
    ) -> None:
        self.returncode = returncode
        self.pid = pid
        self._stdout = stdout
        self._stderr = stderr
        self._timeout_first = timeout_first
        self.calls: list[int | None] = []

    def communicate(self, timeout: int | None = None):
        self.calls.append(timeout)
        if self._timeout_first and len(self.calls) == 1:
            raise subprocess.TimeoutExpired("cmd", timeout, output=self._stdout, stderr=self._stderr)
        return self._stdout, self._stderr


def _command(payload: dict[str, Any], *, deadline_ms: int | None = None) -> Command:
    return Command(id="cmd-exec-1", type="exec", payload=payload, deadline_ms=deadline_ms, ts_ms=1)


def _run(handler_result: Any) -> Any:
    return asyncio.run(handler_result)


def test_exec_enabled_gate_is_off_by_default() -> None:
    handler = make_exec_handler(settings_provider=dict)  # 空设置：必须禁止
    result = _run(handler(_command({"command": "whoami"})))
    assert result.ok is False
    assert result.error_code == "UNSUPPORTED"
    assert "exec_enabled" in (result.error or "")


def test_exec_runs_command_and_returns_exit_code() -> None:
    seen: dict[str, Any] = {}

    def fake_popen(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return _FakeProcess(returncode=0, stdout="nt authority\\system\r\n".encode("utf-8"))

    handler = make_exec_handler(settings_provider=lambda: {"exec_enabled": True}, popen=fake_popen)
    result = _run(handler(_command({"command": "whoami", "timeout_s": 30})))
    assert result.ok is True
    assert result.result["exit_code"] == 0
    assert result.result["stdout"].strip() == "nt authority\\system"
    assert result.result["timed_out"] is False
    assert seen["args"] == "whoami"
    assert seen["kwargs"]["shell"] is True and seen["kwargs"]["stdout"] == subprocess.PIPE
    assert "creationflags" in seen["kwargs"]


def test_exec_missing_command_is_bad_request() -> None:
    handler = make_exec_handler(settings_provider=lambda: {"exec_enabled": True})
    for payload in ({}, {"command": "   "}):
        result = _run(handler(_command(payload)))
        assert result.ok is False and result.error_code == "BAD_REQUEST", payload


def test_exec_timeout_kills_tree_and_returns_partial_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时必须 taskkill /T /F（真机事故回归：孙进程握管道挂死分发循环）。"""

    killed: list[int] = []
    monkeypatch.setattr("observatory_agent.tasks.exec._kill_tree", lambda pid: killed.append(pid))
    process = _FakeProcess(stdout=b"partial ", stderr=b"boom", timeout_first=True)

    handler = make_exec_handler(
        settings_provider=lambda: {"exec_enabled": True},
        popen=lambda *a, **k: process,
    )
    result = _run(handler(_command({"command": "ping -n 100 host", "timeout_s": 5})))
    assert result.ok is False
    assert result.error_code == "TIMEOUT"
    assert result.result["timed_out"] is True
    assert result.result["stdout"] == "partial "
    assert result.result["stderr"] == "boom"
    assert killed == [process.pid], "超时必须杀整棵进程树"
    # 第一次 communicate(5) 超时、杀树后第二次 communicate(5) 收尾
    assert process.calls == [5, 5]


def test_exec_deadline_budget_overrides_requested_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """envelope 预算比 payload 更紧时按预算杀（A6：预算是硬上限）。"""

    seen: dict[str, Any] = {}

    def fake_popen(args, **kwargs):
        class _P:
            pid = 1
            returncode = 0

            def communicate(self, timeout=None):
                seen["timeout"] = timeout
                return b"ok", b""

        return _P()

    monkeypatch.setattr("observatory_agent.tasks.exec._kill_tree", lambda pid: None)
    handler = make_exec_handler(
        settings_provider=lambda: {"exec_enabled": True},
        popen=fake_popen,
        now_ms=lambda: 1_000 + 2_000,  # ts_ms=1 → 已过去 2s
    )
    _run(handler(_command({"command": "cmd", "timeout_s": 600}, deadline_ms=10_000)))
    # 剩余预算 = (1+10000) - 3000 = 7001ms → int(7.001) = 7s，必须覆盖 payload 的 600s
    assert seen["timeout"] == 7, seen["timeout"]


def test_exec_output_truncation_flags() -> None:
    big = "x" * (300 * 1024)

    def fake_popen(args, **kwargs):
        return _FakeProcess(returncode=0, stdout=big.encode("utf-8"), stderr=("e" * (70 * 1024)).encode("utf-8"))

    handler = make_exec_handler(settings_provider=lambda: {"exec_enabled": True}, popen=fake_popen)
    result = _run(handler(_command({"command": "big"})))
    assert result.ok is True
    assert result.result["stdout_truncated"] is True
    assert result.result["stderr_truncated"] is True
    assert len(result.result["stdout"].encode("utf-8")) <= 256 * 1024
    assert len(result.result["stderr"].encode("utf-8")) <= 64 * 1024


def test_clamp_timeout_s_range() -> None:
    assert clamp_timeout_s(None) == 60
    assert clamp_timeout_s("abc") == 60
    assert clamp_timeout_s(1) == 5
    assert clamp_timeout_s(9999) == 600
    assert clamp_timeout_s(120) == 120


def test_truncate_reports_flag() -> None:
    text, flag = truncate("ab", 10)
    assert (text, flag) == ("ab", False)
    text, flag = truncate("abcdef", 4)
    assert flag is True and len(text.encode("utf-8")) <= 4


def test_run_command_captures_real_echo() -> None:
    """唯一一个真 subprocess 用例：跨平台 echo，验证 Popen 路径的真实装配。"""

    outcome = run_command("echo hello", 10)
    assert outcome["exit_code"] == 0
    assert "hello" in outcome["stdout"]
    assert outcome["timed_out"] is False


def test_runtime_wiring_uses_monotonic_clock_for_budget() -> None:
    """真机事故回归（0.3.20）：信封 ts_ms 是单调时钟基准，exec 处理器必须用同源时钟算预算。

    事故机制：注册时注入了墙钟 now_ms，remaining_ms(墙钟) 对单调 ts_ms 算出 ≈0，
    60s 预算被钳到下限 5s——所有 ≥5s 的命令全部 TIMEOUT（exit 0 也白搭）。
    本测试走 ServiceRuntime 的真实注册路径，断言 60s 预算原样传到 run_command。
    """

    import sys

    sys.path.insert(0, "agent")
    import asyncio

    from observatory_agent.core import runtime as runtime_module
    from observatory_agent.tasks import exec as exec_module

    runtime = runtime_module.ServiceRuntime(service_mode=False)
    runtime._register_handlers()
    runtime._store.config.settings["exec_enabled"] = True

    seen: dict[str, Any] = {}

    def fake_run_command(command: str, timeout_s: int, **kwargs):
        seen["timeout"] = timeout_s
        return {
            "exit_code": 0, "stdout": "ok", "stderr": "",
            "stdout_truncated": False, "stderr_truncated": False,
            "duration_ms": 1, "timed_out": False,
        }

    original = exec_module.run_command
    exec_module.run_command = fake_run_command
    try:
        command = runtime_module.Command(
            id="wiring-1",
            type="exec",
            payload={"command": "slow-but-under-budget", "timeout_s": 60},
            deadline_ms=60_000,
            ts_ms=runtime_module._monotonic_ms(),  # 与处理器同源的单调时钟
        )
        result = asyncio.run(runtime._dispatcher.dispatch(command))
    finally:
        exec_module.run_command = original
    assert result.ok is True, result.error
    # 允许构造到执行之间的真实流逝（≈1s）；旧 bug 会把预算钳到下限 5
    assert seen.get("timeout", 0) >= 50, f"60s 预算被错误钳制: {seen}（时钟源错配未修）"
