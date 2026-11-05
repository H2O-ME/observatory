"""0.3.18：``exec`` 命令处理器（SYSTEM 身份、服务进程执行）。

关键契约：
- ``exec_enabled`` 关闭（或缺省）时**绝不执行**，回 UNSUPPORTED——这是任意命令
  执行功能的安全总闸，必须有测试钉死"默认关"。
- 子进程超时回 TIMEOUT 且带回已捕获的部分输出（排障需要）。
- stdout/stderr 截断带标志（回执要走 1MiB 信封）。
- ``run`` 参数注入替身，测试绝不真跑 shell。
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


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return _FakeCompleted(0, stdout="nt authority\\system\r\n".encode("utf-8"), stderr=b"")

    handler = make_exec_handler(settings_provider=lambda: {"exec_enabled": True}, run=fake_run)
    result = _run(handler(_command({"command": "whoami", "timeout_s": 30})))
    assert result.ok is True
    assert result.result["exit_code"] == 0
    assert result.result["stdout"].strip() == "nt authority\\system"
    assert result.result["timed_out"] is False
    assert seen["args"] == "whoami"
    assert seen["kwargs"]["shell"] is True and seen["kwargs"]["timeout"] == 30
    assert "creationflags" in seen["kwargs"]


def test_exec_missing_command_is_bad_request() -> None:
    handler = make_exec_handler(settings_provider=lambda: {"exec_enabled": True})
    for payload in ({}, {"command": "   "}):
        result = _run(handler(_command(payload)))
        assert result.ok is False and result.error_code == "BAD_REQUEST", payload


def test_exec_timeout_returns_partial_output() -> None:
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 5, output=b"partial ", stderr=b"boom")

    handler = make_exec_handler(settings_provider=lambda: {"exec_enabled": True}, run=fake_run)
    result = _run(handler(_command({"command": "ping -n 100 host", "timeout_s": 5})))
    assert result.ok is False
    assert result.error_code == "TIMEOUT"
    assert result.result["timed_out"] is True
    assert result.result["stdout"] == "partial "
    assert result.result["stderr"] == "boom"


def test_exec_deadline_budget_overrides_requested_timeout() -> None:
    """envelope 预算比 payload 更紧时按预算杀（A6：预算是硬上限）。"""

    seen: dict[str, Any] = {}

    def fake_run(args, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return _FakeCompleted(0)

    handler = make_exec_handler(
        settings_provider=lambda: {"exec_enabled": True},
        run=fake_run,
        now_ms=lambda: 1_000 + 2_000,  # ts_ms=1 → 已过去 2s
    )
    _run(handler(_command({"command": "cmd", "timeout_s": 600}, deadline_ms=10_000)))
    # 剩余预算 = (1+10000) - 3000 = 7001ms → int(7.001) = 7s，必须覆盖 payload 的 600s
    assert seen["timeout"] == 7, seen["timeout"]


def test_exec_output_truncation_flags() -> None:
    big = "x" * (300 * 1024)

    def fake_run(args, **kwargs):
        return _FakeCompleted(0, stdout=big.encode("utf-8"), stderr=("e" * (70 * 1024)).encode("utf-8"))

    handler = make_exec_handler(settings_provider=lambda: {"exec_enabled": True}, run=fake_run)
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
    """唯一一个真 subprocess 用例：跨平台 echo，验证 run_command 的真实装配。"""

    outcome = run_command("echo hello", 10)
    assert outcome["exit_code"] == 0
    assert "hello" in outcome["stdout"]
    assert outcome["timed_out"] is False
