"""真机回归：卸载 helper 存活探测必须问自己的子进程句柄。

2026-10-30 生产实测（OBS-1145E10A1191 / ser828735321247）：点“远程卸载”后设备
上报 ``卸载 helper 在确认前退出（pid=4868，日志尾部：<空>）``，控制台一直停在
“正在执行远程卸载”，10 分钟后变成“未确认”。而机器实际上被 helper 清理掉了。

根因：``_process_alive`` 用 ``win32process.SYNCHRONIZE``——pywin32 的 win32process
**不导出**这个名字，``AttributeError`` 被 ``except Exception`` 吞成“进程已退出”。
于是 helper 刚 spawn 出来、还没写状态文件的那一瞬间就被判死（连续两次误报：误报
失败 + helper 照旧继续清理）。

这里的用例全部不到一秒，且第 1 条在旧实现上必然失败（对当前进程都返回 False）。
"""

from __future__ import annotations

import sys
import types

import pytest

from observatory_agent.tasks.uninstall import (
    PurgeTicket,
    UninstallError,
    _process_alive,
    status_document,
    wait_for_ready_status,
    write_status,
)

pytestmark = pytest.mark.unit


def test_alive_probe_with_child_poll_is_authoritative() -> None:
    """有 ``Popen.poll`` 时以它为准：None=还活着，退出码=已退出。"""

    assert _process_alive(4321, child_poll=lambda: None) is True
    assert _process_alive(4321, child_poll=lambda: 0) is False
    assert _process_alive(4321, child_poll=lambda: 2) is False


def test_alive_probe_survives_a_broken_poll() -> None:
    """句柄拿不到时退回 PID 探测，不能因此判死。"""

    import os  # noqa: PLC0415

    def broken() -> int | None:
        raise OSError("handle closed")

    assert _process_alive(os.getpid(), child_poll=broken) is True


@pytest.mark.windows_only
def test_alive_probe_sees_its_own_process() -> None:
    """当前进程当然活着——旧实现连这条都过不了（AttributeError → False）。"""

    import os  # noqa: PLC0415

    assert _process_alive(os.getpid()) is True


def _fake_win32(monkeypatch, *, open_error: BaseException | None = None, wait_result: int = 0x102):
    """装一套假的 win32 模块；``win32process`` **故意不提供**（旧实现依赖它）。"""

    calls: dict[str, object] = {}

    def close_handle(handle: object) -> None:
        calls["closed"] = handle

    def open_process(access: int, inherit: bool, pid: int):  # noqa: ANN001, ANN202
        calls["access"] = access
        calls["pid"] = pid
        if open_error is not None:
            raise open_error
        return "HANDLE"

    win32api = types.SimpleNamespace(OpenProcess=open_process, CloseHandle=close_handle)
    win32event = types.SimpleNamespace(WaitForSingleObject=lambda handle, ms: wait_result, WAIT_OBJECT_0=0)
    # 关键：刻意不给 SYNCHRONIZE，模拟 pywin32 的真实导出面。
    win32con = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "win32api", win32api)
    monkeypatch.setitem(sys.modules, "win32event", win32event)
    monkeypatch.setitem(sys.modules, "win32con", win32con)
    monkeypatch.setitem(sys.modules, "win32process", types.SimpleNamespace())
    return calls


class _WinError(Exception):
    def __init__(self, winerror: int) -> None:
        super().__init__(winerror, "OpenProcess", "失败")
        self.winerror = winerror


def test_alive_probe_does_not_need_win32process_synchronize(monkeypatch) -> None:
    """不依赖 ``win32process.SYNCHRONIZE``：掩码来自 win32con 或内置常量。"""

    calls = _fake_win32(monkeypatch, wait_result=0x102)  # WAIT_TIMEOUT → 仍活着
    monkeypatch.setattr(sys, "platform", "win32")
    assert _process_alive(4868) is True
    assert calls["access"] == 0x00100000
    assert calls["closed"] == "HANDLE"


def test_alive_probe_treats_access_denied_as_alive(monkeypatch) -> None:
    """没权限看 ≠ 已退出（宁可不报错，也不能把成功当失败）。"""

    _fake_win32(monkeypatch, open_error=_WinError(5))
    monkeypatch.setattr(sys, "platform", "win32")
    assert _process_alive(4868) is True


def test_alive_probe_reports_missing_process_as_dead(monkeypatch) -> None:
    """PID 不存在（87）才是真的退出了。"""

    _fake_win32(monkeypatch, open_error=_WinError(87))
    monkeypatch.setattr(sys, "platform", "win32")
    assert _process_alive(4868) is False


def test_confirmation_waits_instead_of_declaring_exit(tmp_path) -> None:
    """状态文件晚一步出现也不能叫“确认前退出”（真机现场的时序）。"""

    status = tmp_path / "uninstall-status.json"
    reads = {"n": 0}

    def fake_read(path):  # noqa: ANN001, ANN202
        reads["n"] += 1
        return None if reads["n"] < 3 else {"ok": True, "pid": 4868, "ts_ms": 1, "error": None}

    import observatory_agent.tasks.uninstall as module  # noqa: PLC0415

    original = module.read_status
    module.read_status = fake_read  # type: ignore[assignment]
    try:
        document = wait_for_ready_status(
            status,
            expected_pid=4868,
            child_poll=lambda: None,  # helper 一直活着
            timeout_s=1.0,
            sleep=lambda _s: None,
        )
    finally:
        module.read_status = original  # type: ignore[assignment]
    assert document["ok"] is True


def test_early_exit_message_carries_exit_code(tmp_path) -> None:
    """真退出了：报错里要有退出码，现场才知道是崩在哪一步。"""

    status = tmp_path / "uninstall-status.json"
    with pytest.raises(UninstallError) as caught:
        wait_for_ready_status(
            status,
            expected_pid=4868,
            child_poll=lambda: 2,
            timeout_s=1.0,
            sleep=lambda _s: None,
        )
    assert "退出码=2" in caught.value.reason


def test_status_file_roundtrip_still_works(tmp_path) -> None:
    """helper 侧契约没被改动：ok=true 能被父进程读到。"""

    status = tmp_path / "uninstall-status.json"
    write_status(status, status_document(ok=True, pid=1234, ts_ms=99))
    document = wait_for_ready_status(status, expected_pid=1234, timeout_s=1.0, sleep=lambda _s: None)
    assert document == {"ok": True, "pid": 1234, "ts_ms": 99, "error": None}


def test_purge_ticket_carries_poll_attribute() -> None:
    """``spawn_purge_helper`` 把 ``Popen.poll`` 装进 ticket，供存活判定使用。"""

    ticket = PurgeTicket(
        pid=1,
        staged_dir=None,  # type: ignore[arg-type]
        confirm_file=None,  # type: ignore[arg-type]
        log_path=None,  # type: ignore[arg-type]
        child_poll=lambda: None,
    )
    assert ticket.child_poll is not None and ticket.child_poll() is None
