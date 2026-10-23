#!/usr/bin/env python3
"""Windows 系统层真实冒烟（W1 A1/A2/A16/A17 的本机可跑部分）。

一条命令跑完：真实命名管道 + 身份校验 + DACL、真实 WTS 会话枚举、真实
虚拟桌面/DPI、真实 SendInput、真实 12 个文件操作、真实更新目录交换/回滚、
真实 onedir 整目录 staging + purge helper 启动（只到"写 ok=true"就终止，
**绝不真正 purge**）。

用法::

    python tools/win_smoke.py            # 跑全部
    python tools/win_smoke.py --verbose  # 失败时附带异常栈
    python tools/win_smoke.py --only pipe
    python tools/win_smoke.py --json

退出码：0 = 没有 FAIL（SKIP 不算失败）；1 = 有 FAIL。
安全红线：本脚本不安装/停止/删除 Windows 服务，不改注册表，不碰系统目录。
所有写操作都在 ``<repo>/.win-smoke/<uuid>/`` 内，结束时删除。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "agent"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


class Skipped(Exception):
    """本机条件下无法验证（会在输出里明确写原因，不计为失败）。"""


@dataclass
class Ctx:
    root: Path
    user_sid: str
    session_id: int
    admin: bool
    restricted_token: bool
    low_integrity: bool
    pyver: str
    notes: dict[str, str] = field(default_factory=dict)
    cleanup: list[Callable[[], None]] = field(default_factory=list)


@dataclass
class Outcome:
    name: str
    status: str
    detail: str
    elapsed_s: float


CHECKS: list[tuple[str, Callable[[Ctx], str]]] = []


def check(name: str) -> Callable[[Callable[[Ctx], str]], Callable[[Ctx], str]]:
    def register(function: Callable[[Ctx], str]) -> Callable[[Ctx], str]:
        CHECKS.append((name, function))
        return function

    return register


# ------------------------------------------------------------------ helpers --
def emit(line: str = "") -> None:
    stream = sys.stdout
    with contextlib.suppress(Exception):
        stream.write(line + "\n")
        stream.flush()


def wait_until(predicate: Callable[[], bool], *, timeout_s: float, interval_s: float = 0.05) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return bool(predicate())


def win32():
    import win32api
    import win32con
    import win32file
    import win32pipe
    import win32security
    import win32ts

    return win32api, win32con, win32file, win32pipe, win32security, win32ts


def current_user_sid() -> str:
    win32api, win32con, _f, _p, win32security, _t = win32()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)
        return str(win32security.ConvertSidToStringSid(user[0]))
    finally:
        win32api.CloseHandle(token)


def token_facts() -> tuple[bool, bool]:
    """(是否受限令牌, 是否低完整性)。"""

    win32api, win32con, _f, _p, win32security, _t = win32()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        restricted = bool(win32security.GetTokenInformation(token, win32security.TokenRestrictedSids))
        try:
            integrity, _attrs = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)
            rid = int(str(win32security.ConvertSidToStringSid(integrity)).rsplit("-", 1)[-1])
            low = rid < 0x2000
        except Exception:  # noqa: BLE001 - 取不到完整性级别就不下结论
            low = False
        return restricted, low
    finally:
        win32api.CloseHandle(token)


def everyone_security_attributes() -> Any:
    """**仅供本脚本**在受限令牌沙箱里验证管道双工：DACL 含 Everyone 才能通过受限 SID 检查。"""

    import ntsecuritycon

    _a, _c, _f, _p, win32security, _t = win32()
    dacl = win32security.ACL()
    for sid_text in ("S-1-1-0", "S-1-5-18", "S-1-5-32-544"):
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            ntsecuritycon.FILE_ALL_ACCESS,
            win32security.ConvertStringSidToSid(sid_text),
        )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def readonly_client(name: str, *, timeout_s: float = 8.0) -> Any:
    _a, _c, win32file, _p, _s, _t = win32()
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return win32file.CreateFile(
                name,
                win32file.GENERIC_READ,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
        except Exception as exc:  # noqa: BLE001 - 服务器实例还在创建中，重试
            last = exc
            time.sleep(0.05)
    raise AssertionError(f"无法以只读客户端连接管道 {name}: {last}")


def make_fake_onedir(destination: Path) -> Path:
    """构造一个假的 onedir 安装根/exe（用 python.exe 冒充 agent exe）。"""

    from observatory_agent.tasks.update import EXE_NAME

    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sys.executable, destination / EXE_NAME)
    internal = destination / "_internal"
    internal.mkdir(exist_ok=True)
    (internal / "py.dll").write_bytes(b"fake-dll")
    return destination


def make_archive(payload_root: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(payload_root.rglob("*")):
            if path.is_file():
                bundle.write(path, str(path.relative_to(payload_root)))


def python_version() -> str:
    completed = subprocess.run(
        [sys.executable, "--version"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return (completed.stdout or completed.stderr).decode("utf-8", "replace").strip()


def pipe_session_id() -> int:
    """随机的管道名后缀：不占用真实会话名，机器上即使装了真实服务也不冲突。"""

    return 500_000 + random.randint(0, 400_000)


# ------------------------------------------------------------------ checks ---
@check("environment")
def check_environment(ctx: Ctx) -> str:
    import importlib.metadata

    version = sys.getwindowsversion()
    import pywintypes  # noqa: F401  （pywin32 的运行时模块，缺它所有 Win32 检查都会失败）
    assert pywintypes.__file__, "pywintypes 不可用"
    try:
        pywin32_version = importlib.metadata.version("pywin32")
    except importlib.metadata.PackageNotFoundError:
        pywin32_version = "?"

    return (
        f"{platform.platform()} build={version.build} "
        f"python={platform.python_version()} pywin32={pywin32_version} "
        f"admin={ctx.admin} restricted={ctx.restricted_token} low_il={ctx.low_integrity} "
        f"sid={ctx.user_sid} session={ctx.session_id}"
    )


@check("imports")
def check_imports(ctx: Ctx) -> str:
    from observatory_agent.core import config, envelope
    from observatory_agent.tasks import fs, input as input_task, uninstall, update
    from observatory_agent.win import pipe, service, session

    collected = [
        config.SERVICE_NAME,
        envelope.PROTOCOL_VERSION,
        pipe.PIPE_PREFIX,
        service.NOT_STARTED_BY_SCM,
        session.CAPTURABLE_STATES,
        fs.SUPPORTED_OPS,
        input_task.ABSOLUTE_MAX,
        update.INSTALL_ROOT_MARKER,
        uninstall.UNINSTALL_STATUS_FILENAME,
    ]
    assert isinstance(envelope.PROTOCOL_VERSION, int), "PROTOCOL_VERSION 不是 int"
    assert pipe.pipe_name(3) == f"{pipe.PIPE_PREFIX}-3", "pipe_name 与 PIPE_PREFIX 不一致"
    return f"9 个模块可导入；{len(collected)} 个常量可读；protocol v{envelope.PROTOCOL_VERSION}"


@check("pipe_acl")
def check_pipe_acl(ctx: Ctx) -> str:
    import ntsecuritycon

    _a, _c, win32file, win32pipe, win32security, _t = win32()
    from observatory_agent.win.pipe import pipe_name, pipe_security_attributes

    name = pipe_name(900_000 + random.randint(0, 99_999))
    attributes = pipe_security_attributes(allowed_user_sid=ctx.user_sid)
    handle = win32pipe.CreateNamedPipe(
        name,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1,
        65_536,
        65_536,
        0,
        attributes,
    )
    try:
        descriptor = win32security.GetSecurityInfo(
            handle, win32security.SE_KERNEL_OBJECT, win32security.DACL_SECURITY_INFORMATION
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        assert dacl is not None, "管道没有 DACL（等于默认放开）"
        entries = []
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            sid = str(win32security.ConvertSidToStringSid(ace[2]))
            entries.append((sid, int(ace[1])))
    finally:
        win32file.CloseHandle(handle)

    sids = {sid for sid, _mask in entries}
    assert "S-1-5-18" in sids, "DACL 缺少 SYSTEM"
    assert "S-1-5-32-544" in sids, "DACL 缺少 Administrators"
    assert ctx.user_sid in sids, f"DACL 缺少当前用户 {ctx.user_sid}"
    assert "S-1-1-0" not in sids, "DACL 放开了 Everyone（A17 不允许）"
    assert "S-1-5-32-545" not in sids, "DACL 放开了 Users（A17 不允许）"
    for sid, mask in entries:
        if sid == ctx.user_sid:
            assert mask & ntsecuritycon.FILE_GENERIC_WRITE, "当前用户没有写权限"
            assert mask & ntsecuritycon.FILE_GENERIC_READ, "当前用户没有读权限"
    return "DACL 只有 SYSTEM/Administrators/当前用户，无 Everyone/Users；用户读写已授权"


@check("pipe_identity")
def check_pipe_identity(ctx: Ctx) -> str:
    _a, _c, win32file, _p, _s, _t = win32()
    from observatory_agent.win.pipe import PipeServer, pipe_name

    def start_server(pipe_sid: int, expected_session_id: int, expected_image: str | None) -> PipeServer:
        server = PipeServer(
            pipe_sid,
            allowed_user_sid=ctx.user_sid,
            on_message=lambda header, payload: None,
            expected_image_path=expected_image,
            expected_session_id=expected_session_id,
        )
        server.start()
        if not wait_until(lambda: server._handle is not None, timeout_s=5.0):
            server.stop()
            raise AssertionError(f"管道实例没有创建出来（{pipe_name(pipe_sid)}）")
        return server

    pipe_sid = pipe_session_id()

    accepted = start_server(pipe_sid, ctx.session_id, str(Path(sys.executable)))
    client = None
    try:
        client = readonly_client(pipe_name(pipe_sid))
        if not wait_until(lambda: accepted.connected.is_set(), timeout_s=5.0):
            raise AssertionError("服务器没有接受身份合法的客户端")
        identity = accepted.last_client
        assert identity is not None, "last_client 为空"
        assert identity.pid > 0 and identity.session_id == ctx.session_id, f"客户端会话不对: {identity}"
        assert identity.user_sid.upper() == ctx.user_sid.upper(), f"客户端 SID 不对: {identity}"
        assert identity.image_path.lower() == str(Path(sys.executable)).lower(), (
            f"客户端镜像路径不对: {identity.image_path}"
        )
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                win32file.CloseHandle(client)
        accepted.stop()

    # 镜像路径不匹配：会话与 SID 都对，只错镜像 → 必须拒绝。
    rejected = start_server(pipe_sid + 1, ctx.session_id, str(Path(sys.executable).with_name("not-the-worker.exe")))
    client = None
    try:
        client = readonly_client(pipe_name(pipe_sid + 1))
        time.sleep(1.0)
        assert not rejected.connected.is_set(), "镜像路径不匹配的客户端被接受了"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                win32file.CloseHandle(client)
        rejected.stop()

    # 期望会话号 ≠ 客户端真实会话：必须拒绝（期望值只影响校验，不影响管道名）。
    wrong_pipe = pipe_sid + 2
    wrong_session = start_server(wrong_pipe, ctx.session_id + 1000, None)
    client = None
    try:
        client = readonly_client(pipe_name(wrong_pipe))
        time.sleep(1.0)
        assert not wrong_session.connected.is_set(), "会话号不匹配的客户端被接受了"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                win32file.CloseHandle(client)
        wrong_session.stop()

    return "合法客户端被接受且 PID/会话/SID/镜像一致；镜像不匹配与会话不匹配都被拒绝"


@check("pipe_duplex")
def check_pipe_duplex(ctx: Ctx) -> str:
    from observatory_agent.core.envelope import new_envelope
    from observatory_agent.win.pipe import PipeClient, PipeServer, PipeUnavailable

    # 管道名用独立的随机后缀（不抢占真实会话名）；期望会话仍是本进程真实会话。
    session_id = pipe_session_id()
    received: list[tuple[dict[str, Any], bytes]] = []
    mode = "production-DACL"

    def run(external_attributes: Any | None) -> None:
        server = PipeServer(
            session_id,
            allowed_user_sid=ctx.user_sid,
            on_message=lambda header, payload: received.append((header, payload)),
            expected_image_path=str(Path(sys.executable)),
            expected_session_id=ctx.session_id,
            security_attributes=external_attributes,
        )
        server.start()
        if not wait_until(lambda: server._handle is not None, timeout_s=5.0):
            server.stop()
            raise AssertionError("duplex 管道实例没有创建出来")
        client = PipeClient(session_id)
        try:
            client.connect(timeout_s=10.0)
            assert wait_until(lambda: server.connected.is_set(), timeout_s=5.0), "管道未连接"
            client.send(new_envelope("hello", {"agent_version": "0.3.0"}, ts_ms=1))
            assert wait_until(lambda: bool(received), timeout_s=5.0), "服务器没收到信封"
            header, _payload = received.pop(0)
            assert header.get("t") == "hello" and header.get("v") == 3, header
            server.send(new_envelope("stop", {"reason": "smoke"}, ts_ms=2))
            packet = client.recv()
            assert packet is not None and packet[0].get("t") == "stop", packet
            big = b"x" * (512 * 1024)
            client.send({"t": "frame", "w": 800, "h": 600, "device_ts_ms": 3}, big)
            assert wait_until(lambda: bool(received), timeout_s=10.0), "服务器没收到二进制帧"
            frame_header, frame_payload = received.pop(0)
            assert frame_header.get("t") == "frame", frame_header
            assert int(frame_header.get("len") or -1) == len(big), frame_header
            assert frame_payload == big, "二进制载荷被截断/损坏"
        finally:
            client.close()
            server.stop()

    if ctx.restricted_token or ctx.low_integrity:
        # 沙箱的受限令牌会让生产 DACL（只授权 SYSTEM/Admins/当前用户）过不了受限 SID 检查，
        # 生产 ACL 的“严格性”已由 pipe_acl 验证；这里用 test DACL 验证真实的双工编解码。
        mode = "test-DACL（受限令牌沙箱，生产 DACL 在本机不可写）"
        run(everyone_security_attributes())
    else:
        try:
            run(None)
        except PipeUnavailable as exc:
            mode = "test-DACL（生产 DACL 连接失败后的回退）"
            if "拒绝" not in str(exc) and "denied" not in str(exc).lower() and "(5," not in str(exc):
                raise
            run(everyone_security_attributes())
    return f"真实双工：hello 往返 + server→client + 512KiB 二进制帧（{mode}）"


@check("pipe_stop_reclaim")
def check_pipe_stop_reclaim(ctx: Ctx) -> str:
    """回归：stop() 必须能解开阻塞的 ConnectNamedPipe 并释放管道名（否则服务停不下来/重启失败）。"""

    from observatory_agent.win.pipe import PipeServer, pipe_name

    pipe_sid = pipe_session_id()
    for attempt in range(3):
        server = PipeServer(
            pipe_sid,
            allowed_user_sid=ctx.user_sid,
            on_message=lambda header, payload: None,
            expected_session_id=ctx.session_id,
        )
        server.start()
        assert wait_until(lambda: server._handle is not None, timeout_s=5.0), f"第 {attempt + 1} 次没有创建出管道实例"
        started = time.monotonic()
        server.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"stop() 用了 {elapsed:.1f}s（CloseHandle 未解开 ConnectNamedPipe？）"
        assert server._thread is None or not server._thread.is_alive(), "server 线程没有退出"
    assert pipe_name(pipe_sid).startswith("\\\\.\\pipe\\"), pipe_name(pipe_sid)
    return "同一管道名 create→stop→create 连续 3 次成功，stop() < 5s 且线程退出"
@check("wts_sessions")
def check_wts_sessions(ctx: Ctx) -> str:
    from observatory_agent.win.session import (
        SessionError,
        build_command_line,
        console_session_id,
        executable_path,
        list_sessions,
        select_capturable,
        session_user_sid,
        worker_arguments,
    )

    sessions = list_sessions()
    assert sessions, "WTSEnumerateSessions 返回空列表"
    console = console_session_id()
    assert isinstance(console, int) and console >= 0, f"console_session_id 异常: {console!r}"
    selected = select_capturable(sessions)
    if selected is not None:
        assert any(item.session_id == selected and item.username.strip() for item in sessions), (
            f"select_capturable 选出的会话不在列表/没有用户名: {selected}"
        )
        if any(item.session_id == console and item.username.strip() for item in sessions):
            assert selected == console, f"控制台会话可用时没有优先选它: {selected} != {console}"
    arguments = worker_arguments(7, frozen=False)
    assert arguments[:2] == ["-m", "observatory_agent"], arguments
    assert "--worker" in arguments and arguments[arguments.index("--session") + 1] == "7", arguments
    command_line = build_command_line(executable_path(), arguments)
    assert "observatory_agent" in command_line, command_line

    note = ""
    try:
        sid = session_user_sid(console)
        note = f"; console session_user_sid={sid}"
    except SessionError as exc:
        note = f"; session_user_sid 不可用（非 SYSTEM，需 SE_TCB）：{getattr(exc, 'step', '') or str(exc)[:60]}"

    summary = ", ".join(f"{item.session_id}:{item.username or '-'}:{item.state}" for item in sessions[:4])
    return f"WTS 会话 {len(sessions)} 个（{summary}），console={console}，selected={selected}{note}"


@check("dpi_virtual_desktop")
def check_dpi_virtual_desktop(ctx: Ctx) -> str:
    import ctypes
    from ctypes import wintypes

    from observatory_agent.tasks.input import (
        enable_dpi_awareness,
        normalized_to_absolute,
        to_absolute,
        to_normalized,
        from_normalized,
        virtual_desktop,
        virtual_to_absolute,
    )

    mechanism = enable_dpi_awareness()
    assert mechanism and mechanism != "none", f"DPI 感知设置失败: {mechanism}"

    desktop = virtual_desktop()
    assert desktop.width > 0 and desktop.height > 0, desktop

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    rects: list[tuple[int, int, int, int]] = []
    MonitorEnumProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM
    )

    def callback(hmonitor: Any, hdc: Any, rect_ptr: Any, lparam: Any) -> int:
        rect = rect_ptr.contents
        rects.append((int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)))
        return 1

    assert user32.EnumDisplayMonitors(0, 0, MonitorEnumProc(callback), 0), "EnumDisplayMonitors 失败"
    assert rects, "EnumDisplayMonitors 返回 0 块显示器"
    union_left = min(item[0] for item in rects)
    union_top = min(item[1] for item in rects)
    union_right = max(item[2] for item in rects)
    union_bottom = max(item[3] for item in rects)
    expected = (union_left, union_top, union_right - union_left, union_bottom - union_top)
    actual = (desktop.left, desktop.top, desktop.width, desktop.height)
    assert actual == expected, f"GetSystemMetrics 虚拟桌面 {actual} != EnumDisplayMonitors 并集 {expected}"

    dpi = int(user32.GetDpiForSystem())
    assert 48 <= dpi <= 768, f"GetDpiForSystem 异常: {dpi}"
    try:
        desktop_window = int(user32.GetDesktopWindow())
        window_dpi = int(user32.GetDpiForWindow(desktop_window))
        assert window_dpi > 0, f"GetDpiForWindow 返回 0（窗口句柄 {desktop_window}）"
    except AttributeError:  # 旧系统没有 GetDpiForWindow
        window_dpi = 0

    assert to_absolute(desktop, 0.0, 0.0).x == desktop.left
    assert to_absolute(desktop, 0.0, 0.0).y == desktop.top
    assert to_absolute(desktop, 1.0, 1.0).x == desktop.right - 1
    assert to_absolute(desktop, 1.0, 1.0).y == desktop.bottom - 1
    assert virtual_to_absolute(desktop.left, desktop.top, desktop) == (0, 0)
    assert virtual_to_absolute(desktop.right - 1, desktop.bottom - 1, desktop) == (65535, 65535)
    for value in (0, 1, 5_000, 9_999, 10_000):
        assert abs(from_normalized(to_normalized(value)) - value) <= 1, value
    mid = normalized_to_absolute(0.5, 0.5, desktop)
    assert isinstance(mid, tuple) and len(mid) == 2

    monitors = ", ".join(f"({m.left},{m.top},{m.width}x{m.height},{'primary' if m.primary else 'secondary'})" for m in desktop.monitors)
    return (
        f"dpi={mechanism}；显示器 {len(rects)} 块 [{monitors}]；"
        f"虚拟桌面=({desktop.left},{desktop.top},{desktop.width}x{desktop.height})；"
        f"GetDpiForSystem={dpi} GetDpiForWindow={window_dpi}"
    )


@check("input_send")
def check_input_send(ctx: Ctx) -> str:
    import ctypes
    from ctypes import wintypes

    from observatory_agent.tasks.input import mouse_move, virtual_desktop

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    point = wintypes.POINT()
    assert user32.GetCursorPos(ctypes.byref(point)), "GetCursorPos 失败（无交互桌面？）"
    desktop = virtual_desktop()
    moved = mouse_move(int(point.x), int(point.y), desktop=desktop)
    if not moved:
        raise Skipped("SendInput 被系统拒绝（会话锁屏或无交互桌面），无法验证真实注入")
    return f"SendInput 接受真实鼠标移动（光标留在原位置 {point.x},{point.y}）"


@check("input_events")
def check_input_events(ctx: Ctx) -> str:
    from observatory_agent.tasks.input import (
        event_target,
        normalized_10000_to_virtual,
        text_to_utf16_units,
        virtual_desktop,
    )

    desktop = virtual_desktop()
    pixel = event_target({"kind": "mouse", "x": 100, "y": 200}, desktop=desktop)
    assert pixel == (100, 200), pixel
    norm = event_target({"kind": "mouse", "norm": True, "x": 5_000, "y": 5_000}, desktop=desktop)
    expected = normalized_10000_to_virtual(5_000, 5_000, desktop)
    assert norm == expected, f"norm 解析 {norm} != {expected}"
    unit = event_target({"kind": "mouse", "normalized": True, "x": 0.5, "y": 0.5}, desktop=desktop)
    assert isinstance(unit, tuple) and all(isinstance(value, int) for value in unit), unit
    assert len(text_to_utf16_units("a\U0001F600")) == 3, "emoji 代理对没有展开成 2 个 UTF-16 unit"
    return f"像素/norm(0..10000)/normalized(0..1) 三种约定解析正确；norm 中点={expected}"


@check("fs_operations")
def check_fs_operations(ctx: Ctx) -> str:
    import base64

    from observatory_agent.tasks import fs

    root = ctx.root / "fs"
    root.mkdir(parents=True)
    now = lambda: 42  # noqa: E731 - 注入固定时钟

    def op(name: str, **payload: Any) -> dict[str, Any]:
        payload.setdefault("path", str(root))
        return fs.perform(name, payload, allow_write=True, now_ms=now)

    drives = op("drives")["data"]["drives"]
    assert drives and all("drive" in item for item in drives), drives
    directory = root / "dir"
    assert op("mkdir", path=str(directory))["ok"]
    target = directory / "chunk.txt"
    assert op("write", path=str(target), data=base64.b64encode(b"hello ").decode(), offset=0, truncate=True)["data"]["written"] == 6
    assert op("write", path=str(target), data=base64.b64encode(b"world").decode(), offset=6, truncate=False)["data"] ["size"] == 11
    read_back = op("read", path=str(target))["data"]
    assert base64.b64decode(read_back["data"]) == b"hello world", read_back
    assert read_back["eof"] is True and read_back["size"] == 11
    beyond = op("read", path=str(target), offset=1_000, length=10)["data"]
    assert beyond["length"] == 0 and beyond["eof"] is True, beyond
    assert op("stat", path=str(target))["data"]["size"] == 11
    listing = op("list", path=str(directory))["data"]
    assert any(entry["name"] == "chunk.txt" for entry in listing["entries"]), listing
    copy_path = directory / "copy.txt"
    assert op("copy", path=str(target), target=str(copy_path))["ok"]
    moved = directory / "moved.txt"
    assert op("move", path=str(copy_path), target=str(moved))["ok"]
    renamed = directory / "renamed.txt"
    assert op("rename", path=str(moved), target=str(renamed))["ok"]
    found = op("search", path=str(directory), pattern="*.txt")["data"]["entries"]
    assert {entry["name"] for entry in found} >= {"chunk.txt", "renamed.txt"}, found
    digest = op("hash", path=str(renamed))["data"]["sha256"]
    assert len(digest) == 64, digest
    assert op("delete", path=str(renamed))["ok"] and not renamed.exists()
    sub = directory / "sub"
    assert op("mkdir", path=str(sub))["ok"]
    (sub / "payload.bin").write_bytes(b"x" * 32)
    assert op("delete", path=str(sub), recursive=True)["ok"] and not sub.exists()

    try:
        fs.perform("read", {"path": str(directory / "missing.txt")}, allow_write=False, now_ms=now)
    except fs.FileOpError:
        pass
    else:
        raise AssertionError("读取不存在的文件没有报错")
    for bad_payload in (
        {"op": "write", "path": str(directory / "x.bin"), "offset": 0},  # 缺 data
        {"op": "write", "path": str(directory / "x.bin"), "data": "!!!", "offset": 0},
        {"op": "search", "path": "", "pattern": "*"},
        {"op": "read", "path": "bad\x00path"},
    ):
        if "op" in bad_payload:
            payload = dict(bad_payload)
            name = payload.pop("op")
        else:
            name, payload = "read", bad_payload
        try:
            fs.perform(name, payload, allow_write=False, now_ms=now)
        except fs.FileOpError:
            continue
        raise AssertionError(f"非法文件操作没有报错: {name} {bad_payload}")
    try:
        fs.perform("write", {"path": str(directory / "x.bin"), "data": "aGk=", "offset": 0}, allow_write=False, now_ms=now)
    except fs.FileOpError:
        pass
    else:
        raise AssertionError("allow_write=False 时写入没有被拒绝")

    junction_note = "junction 未测"
    junction_target = root / "junction-target"
    junction_target.mkdir()
    (junction_target / "keep.txt").write_text("keep", encoding="utf-8")
    junction = root / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(junction_target)],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        check=False,
    )
    if created.returncode == 0 and junction.exists():
        assert op("delete", path=str(junction), recursive=True)["ok"]
        assert not junction.exists(), "junction 没删掉"
        assert (junction_target / "keep.txt").is_file(), "删除 junction 时误删了目标目录内容"
        junction_note = "junction 安全删除（目标内容保留）"
    else:
        junction_note = f"junction 未测（mklink rc={created.returncode}）"
    return f"12 个操作 + 分片读写 + 越界/非法输入拒绝；{junction_note}"


@check("update_pipeline")
def check_update_pipeline(ctx: Ctx) -> str:
    import asyncio

    from observatory_agent.tasks import update as up

    root = ctx.root / "update"
    root.mkdir(parents=True)
    pyver = ctx.pyver

    payload_src = root / "payload-src"
    make_fake_onedir(payload_src)
    (payload_src / "new-marker.txt").write_text("new", encoding="utf-8")
    archive = root / "pkg.zip"
    make_archive(payload_src, archive)
    digest = up.sha256_file(archive)
    up.verify_package(archive, digest)

    bad_copy = root / "bad.zip"
    shutil.copy2(archive, bad_copy)
    try:
        up.verify_package(bad_copy, "0" * 64)
    except up.UpdateRejected:
        assert not bad_copy.exists(), "摘要不符的坏包没有被删除"
    else:
        raise AssertionError("摘要不符的包被接受了")

    extracted = root / "extract"
    up.extract_payload(archive, extracted)
    assert (extracted / up.EXE_NAME).is_file() and (extracted / "_internal").is_dir()
    try:
        up.precheck(extracted, "9.9.9")
    except up.UpdateError:
        pass
    else:
        raise AssertionError("precheck 没有拒绝版本不符的 exe")
    assert up.precheck(extracted, pyver) == pyver

    escape_zip = root / "escape.zip"
    with zipfile.ZipFile(escape_zip, "w") as bundle:
        bundle.writestr("../escape.txt", "x")
    try:
        up.extract_payload(escape_zip, root / "escape-out")
    except up.UpdateError:
        pass
    else:
        raise AssertionError("路径穿越的 zip 没有被拒绝")

    async def fetch(destination: Path) -> None:
        shutil.copy2(archive, destination)

    staged = asyncio.run(
        up.stage_update(version=pyver, sha256=digest, fetch=fetch, staging_dir=root / "staging")
    )
    assert (staged / up.EXE_NAME).is_file() and (staged / up.VERSION_FILE).is_file(), staged

    class FakeController:
        def __init__(self, *, fail_first_start: bool = False) -> None:
            self.start_calls = 0
            self.fail_first_start = fail_first_start

        def start(self) -> bool:
            self.start_calls += 1
            return not (self.fail_first_start and self.start_calls == 1)

        def stop(self) -> bool:
            return True

        def wait_for_process_exit(self, pid: int, timeout_s: float) -> bool:
            return True

    install = make_fake_onedir(root / "install")
    (install / "old-marker.txt").write_text("old", encoding="utf-8")
    plan = up.plan_swap(install_dir=install, payload_root=staged, timestamp_ms=123_456)
    up.swap_directories(plan)
    assert (install / "new-marker.txt").is_file() and not (install / "old-marker.txt").exists()
    assert (plan.backup_dir / "old-marker.txt").is_file(), "备份目录缺少旧构建"
    assert up._rollback(plan, FakeController(), sleep=lambda _s: None), "回滚失败"
    assert (install / "old-marker.txt").is_file() and not (install / "new-marker.txt").exists()

    happy_target = make_fake_onedir(root / "happy-target")
    (happy_target / "old-marker.txt").write_text("old", encoding="utf-8")
    happy_payload = root / "happy-payload"
    shutil.copytree(payload_src, happy_payload)
    rc = up.apply_now(
        happy_payload,
        happy_target,
        wait_pid=os.getpid(),
        version=pyver,
        controller=FakeController(),
        now_ms=lambda: 900,
        sleep=lambda _s: None,
    )
    assert rc == up.RC_OK, f"apply_now 正常路径 rc={rc}"
    assert (happy_target / "new-marker.txt").is_file(), "更新后目标目录没有新内容"

    rollback_target = make_fake_onedir(root / "rollback-target")
    (rollback_target / "old-marker.txt").write_text("old", encoding="utf-8")
    rollback_payload = root / "rollback-payload"
    shutil.copytree(payload_src, rollback_payload)
    rc = up.apply_now(
        rollback_payload,
        rollback_target,
        wait_pid=os.getpid(),
        version=pyver,
        controller=FakeController(fail_first_start=True),
        now_ms=lambda: 901,
        sleep=lambda _s: None,
    )
    assert rc == up.RC_ROLLED_BACK, f"启动失败应回滚，rc={rc}"
    assert (rollback_target / "old-marker.txt").is_file() and not (rollback_target / "new-marker.txt").exists()

    helper_payload = root / "helper-payload"
    shutil.copytree(payload_src, helper_payload)
    handle = up.spawn_helper(
        helper_payload,
        install_dir=happy_target,
        staging_dir=root / "helper-staging",
        version=pyver,
        current_pid=os.getpid(),
    )
    try:
        up.verify_helper_alive(handle, grace_s=0.5, sleep=lambda _s: None)
        raise AssertionError("python.exe 冒充的 helper 不应通过存活检查")
    except up.UpdateError:
        pass
    finally:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/PID", str(handle.pid), "/F"],
                capture_output=True,
                timeout=30,
                check=False,
            )

    return (
        "verify(ok/坏包删除)/extract/穿越拒绝/stage(真实 zip)/precheck(真实 exe)/"
        "plan+swap+备份+rollback/apply_now(正常 RC_OK、启动失败 RC_ROLLED_BACK)/spawn_helper(真实进程+存活检测)"
    )


@check("uninstall_staging")
def check_uninstall_staging(ctx: Ctx) -> str:
    from observatory_agent.tasks import uninstall as un

    root = ctx.root / "uninstall"
    fake_install = root / "fake-install"
    fake_install.mkdir(parents=True)
    shutil.copytree(
        AGENT_SRC / "observatory_agent",
        fake_install / "observatory_agent",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(sys.executable, fake_install / un.EXE_NAME)
    (fake_install / "_internal").mkdir()
    (fake_install / "_internal" / "py.dll").write_bytes(b"dll")
    data_dir = root / "data"
    data_dir.mkdir()

    staged = un.stage_helper(fake_install, staging_root=root / "staging", now_ms=lambda: 654_321)
    source_files = sorted(
        str(path.relative_to(fake_install)) for path in fake_install.rglob("*") if path.is_file()
    )
    staged_files = sorted(str(path.relative_to(staged)) for path in staged.rglob("*") if path.is_file())
    assert staged_files == source_files, "整目录 staging 的文件集合与安装目录不一致"
    assert (staged / un.EXE_NAME).is_file() and (staged / "_internal").is_dir()
    assert (staged / "observatory_agent" / "main.py").is_file(), "staged 副本缺少可运行的包"

    command = un.purge_command(
        staged,
        install_dir=fake_install,
        data_dir=data_dir,
        wait_pid=os.getpid(),
        confirm_file=data_dir / un.UNINSTALL_STATUS_FILENAME,
    )
    assert command[0].endswith(un.EXE_NAME) and "--purge-helper" in command
    for flag in ("--wait-pid", "--install-dir", "--data-dir", "--confirm-file"):
        assert flag in command, f"helper 命令行缺少 {flag}"

    status_path = data_dir / un.UNINSTALL_STATUS_FILENAME
    env = os.environ.copy()
    env["PYTHONPATH"] = str(staged)
    env["OBS_PROGRAM_DATA"] = str(data_dir)
    log_path = root / "helper.log"
    child: subprocess.Popen[bytes] | None = None
    try:
        with open(log_path, "wb") as log_handle:
            child = subprocess.Popen(
                [sys.executable, "-m", "observatory_agent", *command[1:]],
                cwd=str(staged),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        assert wait_until(lambda: status_path.is_file(), timeout_s=30.0), (
            f"helper 没有写回状态文件；日志：{log_path.read_text(encoding='utf-8', errors='replace')[-800:]}"
        )
        time.sleep(0.2)
        document = json.loads(status_path.read_text(encoding="utf-8"))
        assert document.get("ok") is True, document
        assert int(document.get("pid") or 0) == child.pid, document
        assert child.poll() is None, "helper 写完确认后就退出了"
        document_seen = document
    finally:
        if child is not None and child.poll() is None:
            with contextlib.suppress(Exception):
                child.kill()
        if child is not None:
            with contextlib.suppress(Exception):
                child.wait(timeout=10)
    assert fake_install.is_dir(), "helper 在确认阶段就动了安装目录（不应发生）"
    assert status_path.is_file(), "状态文件在 helper 终止后消失"
    return (
        f"整目录 staging {len(staged_files)} 个文件（含 _internal/ 与包）/ helper 真实启动 pid={document_seen['pid']} "
        "并写回 ok=true；写回后即终止，未执行 purge"
    )


@check("service_readonly")
def check_service_readonly(ctx: Ctx) -> str:
    from datetime import datetime

    from observatory_agent.win.service import (
        SERVICE_RUNNING,
        SERVICE_STOPPED,
        ServiceController,
        _quoted_binary_path,
        plan_restart,
        restart_task_xml,
    )

    status = ServiceController().status()
    state_name = {SERVICE_RUNNING: "running", SERVICE_STOPPED: "stopped"}.get(status.state, str(status.state))
    plan = plan_restart(now=datetime.now(), delay_s=10)
    xml = restart_task_xml(plan)
    assert "<TimeTrigger>" in xml and "<StartBoundary>" in xml and "StartWhenAvailable" in xml
    quoted = _quoted_binary_path(Path(sys.executable))
    assert quoted.startswith('"') and quoted.endswith('"'), quoted
    return f"只读查询 SCM 成功（state={state_name}）；一次性重启任务 XML/路径引号可生成；未做任何写操作"


@check("service_scm_write")
def check_service_scm_write(ctx: Ctx) -> str:
    raise Skipped(
        "安全红线：win_smoke 不安装/停止/删除服务。A2 的 SCM recovery 实际触发与 install_service "
        "需在干净 Windows VM 上人工验证（管理员 + 服务未运行时）"
    )


@check("createprocessasuser")
def check_createprocessasuser(ctx: Ctx) -> str:
    raise Skipped(
        "需要 SYSTEM 权限（WTSQueryUserToken 要求 SE_TCB，本机非 SYSTEM）；"
        "A1/A17 的跨会话 worker 拉起需在服务上下文中验证"
    )


# ------------------------------------------------------------------- runner --
def run_checks(ctx: Ctx, *, verbose: bool, only: str | None) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for name, function in CHECKS:
        if only and only not in name:
            continue
        started = time.monotonic()
        try:
            detail = function(ctx)
            status = PASS
        except Skipped as exc:
            status, detail = SKIP, str(exc)
        except Exception as exc:  # noqa: BLE001 - 冒烟脚本必须把所有失败变成一行结果
            status = FAIL
            detail = f"{type(exc).__name__}: {exc}"
            if verbose:
                detail += "\n" + traceback.format_exc()
        elapsed = time.monotonic() - started
        outcomes.append(Outcome(name=name, status=status, detail=detail, elapsed_s=elapsed))
        emit(f"{status:4}  {name:<22} {detail}  ({elapsed:.2f}s)")
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Windows 系统层真实冒烟（安全：不碰服务/注册表/系统目录）")
    parser.add_argument("--verbose", action="store_true", help="失败时打印完整异常栈")
    parser.add_argument("--only", default="", help="只跑名字包含该子串的检查")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON 摘要")
    parser.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    args = parser.parse_args(argv)

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]

    if sys.platform != "win32":
        emit("SKIP  win_smoke 需要 Windows；当前平台不是 win32（退出码 0）")
        return 0

    restricted, low = token_facts()
    try:
        _a, _c, _f, _p, _s, win32ts = win32()
        session_id = int(win32ts.ProcessIdToSessionId(os.getpid()))
    except Exception:  # noqa: BLE001 - 取不到会话号时用 0（只影响输出展示）
        session_id = 0
    root = Path(os.environ.get("WIN_SMOKE_ROOT") or (REPO_ROOT / ".win-smoke")) / uuid.uuid4().hex[:12]
    root.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(
        root=root,
        user_sid=current_user_sid(),
        session_id=session_id,
        admin=bool(__import__("ctypes").windll.shell32.IsUserAnAdmin()),
        restricted_token=restricted,
        low_integrity=low,
        pyver=python_version(),
    )

    emit(f"win_smoke  root={root}")
    emit(f"           windows={platform.platform()} python={platform.python_version()} pyver={ctx.pyver}")
    if ctx.restricted_token or ctx.low_integrity:
        emit(
            "           注意：当前进程是受限令牌/低完整性（沙箱环境）；"
            "生产管道 DACL 的写权限会被拒，双工项会退回 test-DACL 验证。"
        )
    emit("")

    outcomes = run_checks(ctx, verbose=args.verbose, only=args.only)
    failed = [item for item in outcomes if item.status == FAIL]
    skipped = [item for item in outcomes if item.status == SKIP]
    passed = [item for item in outcomes if item.status == PASS]
    emit("")
    emit(f"汇总：{len(passed)} PASS / {len(failed)} FAIL / {len(skipped)} SKIP")
    if failed:
        emit("失败项：" + ", ".join(item.name for item in failed))
    if args.json:
        emit(
            json.dumps(
                {
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "admin": ctx.admin,
                    "restricted_token": ctx.restricted_token,
                    "low_integrity": ctx.low_integrity,
                    "results": [
                        {"name": item.name, "status": item.status, "detail": item.detail, "elapsed_s": round(item.elapsed_s, 3)}
                        for item in outcomes
                    ],
                    "exit_code": 1 if failed else 0,
                },
                ensure_ascii=False,
            )
        )

    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())