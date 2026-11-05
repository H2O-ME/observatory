"""远程命令执行（``exec`` 命令，0.3.18）。

设计要点
--------
- **注册在服务进程 dispatcher**：服务跑在 SYSTEM，这是受控端最高权限，且无人
  登录/锁屏时照样可用（worker 只活在交互会话里）。明确放弃低权沙箱——这是
  产品决策（0.3.18，用户确认"受控端最高什么权限就什么权限"）。
- **总开关** ``exec_enabled``：默认关闭（服务端 settings_spec 与本地
  DEFAULT_SETTINGS 双侧默认 False），在控制台按设备/全局打开后才会执行。
  仿 :mod:`tasks.fs` 的 ``file_write_enabled`` 门——开关缺失即禁止，不静默放行。
- **预算**：服务端把 ``deadline_ms`` 按 payload 的 ``timeout_s`` 派生（见
  ``console/devices.py``），这里的子进程超时直接用 payload 值；同时用
  ``command.remaining_ms()`` 兜底（到点强杀，不许命令绕过预算跑飞）。
- **输出**：stdout 截断 256KB / stderr 截断 64KB，带 ``*_truncated`` 标志
  （管道/信封上限 1MiB，回执还要装 JSON，留足余量）。解码先 utf-8、失败转
  mbcs——中文 Windows 的 cmd 输出是 cp936。
- **可测性**：``run`` 参数注入 subprocess 替身；时间用 ``now_ms`` 注入。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

from ..core.commands import (
    ERROR_BAD_REQUEST,
    ERROR_TIMEOUT,
    ERROR_UNSUPPORTED,
    Command,
    CommandResult,
)

log = logging.getLogger(__name__)

#: payload ``timeout_s`` 的合法区间（秒）。上限 600：服务端行过期 = 预算×3，
#: 600s 命令的回执窗口 30 分钟，轮询方完全等得起。
MIN_TIMEOUT_S: Final[int] = 5
MAX_TIMEOUT_S: Final[int] = 600
DEFAULT_TIMEOUT_S: Final[int] = 60

#: 输出截断（字节）。两者之和远小于 MAX_ENVELOPE_BYTES(1MiB)。
MAX_STDOUT_BYTES: Final[int] = 256 * 1024
MAX_STDERR_BYTES: Final[int] = 64 * 1024


def default_now_ms() -> int:
    return int(time.time() * 1000)


def clamp_timeout_s(value: Any, default: int = DEFAULT_TIMEOUT_S) -> int:
    """``timeout_s`` 钳制到 5..600；类型错回落默认值。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, number))


def decode_output(data: bytes | None) -> str:
    """子进程输出解码：utf-8 失败转 mbcs（中文 Windows cmd 的 cp936）。"""

    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("mbcs", "replace")


def truncate(data: str, limit: int) -> tuple[str, bool]:
    """按字节截断字符串；返回 (内容, 是否截断)。"""

    encoded = data.encode("utf-8")
    if len(encoded) <= limit:
        return data, False
    return encoded[:limit].decode("utf-8", "ignore"), True


def _kill_tree(pid: int) -> None:
    """taskkill /T /F：杀掉整棵进程树。

    真机事故（2026-11-05）：subprocess.run 超时只杀直接子进程（cmd.exe），
    常驻循环的 powershell 孙进程继续握着 stdout/stderr 管道，收尾的管道读取
    永久阻塞 → 处理线程挂死 → 分发循环冻结 → 设备命令通道全灭 1.5 小时。
    """

    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001 - 杀树失败不能阻塞回执
        log.warning("taskkill /T /F 失败 pid=%s: %s", pid, exc)


def run_command(
    command: str,
    timeout_s: int,
    *,
    now_ms: Callable[[], int] = default_now_ms,
    popen: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """同步执行一条 shell 命令并打包回执（线程池里跑，不阻塞事件循环）。

    超时路径不直接用 ``subprocess.run(timeout=…)``：它超时只杀直接子进程，
    孙进程持有的管道会让收尾读取永久挂死（见 :func:`_kill_tree`）。这里用
    ``Popen`` + ``wait(timeout)``，超时先杀整棵树再受控地收管道。
    """

    started = now_ms()
    timed_out = False
    stdout_data = b""
    stderr_data = b""
    exit_code: int | None = None
    if popen is None:
        popen = subprocess.Popen
    try:
        process = popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001 - 启动失败也要回执
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "stdout_truncated": False,
            "stderr_truncated": False,
            "duration_ms": max(0, now_ms() - started),
            "timed_out": False,
        }
    try:
        stdout_data, stderr_data = process.communicate(timeout=timeout_s)
        exit_code = int(process.returncode or 0)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(process.pid)
        try:
            # 树已杀，管道应立即 EOF；再给 5s 兜底，仍不通就放弃读取。
            stdout_data, stderr_data = process.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - 收不到就带部分输出走
            stdout_data, stderr_data = b"", b""
    duration_ms = max(0, now_ms() - started)
    stdout, stdout_truncated = truncate(decode_output(stdout_data), MAX_STDOUT_BYTES)
    stderr, stderr_truncated = truncate(decode_output(stderr_data), MAX_STDERR_BYTES)
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "duration_ms": duration_ms,
        "timed_out": timed_out,
    }


def make_exec_handler(
    *,
    settings_provider: Callable[[], Mapping[str, Any]],
    now_ms: Callable[[], int] = default_now_ms,
    popen: Callable[..., Any] | None = None,
) -> Callable[[Command], Any]:
    """构造 ``exec`` 命令处理器（注册到服务进程 dispatcher，SYSTEM 身份执行）。"""

    async def handle(command: Command) -> CommandResult:
        if not bool(settings_provider().get("exec_enabled", False)):
            return CommandResult(
                ok=False,
                error="远程命令执行未启用（控制台设置 exec_enabled）",
                error_code=ERROR_UNSUPPORTED,
            )
        text = str(command.payload.get("command") or "").strip()
        if not text:
            return CommandResult(
                ok=False,
                error="exec 命令缺少 command 字段",
                error_code=ERROR_BAD_REQUEST,
            )
        timeout_s = clamp_timeout_s(command.payload.get("timeout_s"))
        # 服务端按 payload 派生预算（timeout_s*1000），这里兜底强杀：即使
        # 预算异常紧也不能让命令跑飞（A6 语义：预算是硬上限）。
        remaining = command.remaining_ms(now_ms())
        if remaining is not None:
            budget_s = max(MIN_TIMEOUT_S, int(remaining / 1000))
            timeout_s = min(timeout_s, budget_s)
        outcome = await asyncio.to_thread(run_command, text, timeout_s, popen=popen, now_ms=now_ms)
        if outcome.get("timed_out"):
            return CommandResult(
                ok=False,
                error=f"命令超时被终止（timeout_s={timeout_s}）",
                error_code=ERROR_TIMEOUT,
                result=outcome,
            )
        return CommandResult(ok=True, result=outcome)

    return handle


def register_handlers(
    dispatcher: Any,
    *,
    settings_provider: Callable[[], Mapping[str, Any]],
    now_ms: Callable[[], int] = default_now_ms,
    popen: Callable[..., Any] | None = None,
) -> None:
    """把 ``exec`` 注册到 :class:`core.commands.CommandDispatcher`。"""

    dispatcher.register(
        "exec",
        make_exec_handler(settings_provider=settings_provider, now_ms=now_ms, popen=popen),
    )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_STDERR_BYTES",
    "MAX_STDOUT_BYTES",
    "MAX_TIMEOUT_S",
    "MIN_TIMEOUT_S",
    "clamp_timeout_s",
    "decode_output",
    "make_exec_handler",
    "register_handlers",
    "run_command",
    "truncate",
]
