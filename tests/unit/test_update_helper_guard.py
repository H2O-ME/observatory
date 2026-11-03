"""2026-11-03 事故回归：helper 判活误报、强停活服务、staging 日志丢失。

锁定四条纪律：
1. ``_process_alive`` 不依赖 win32api 单条腿（psutil/ctypes 兜底）；
2. helper 存活窗口内退出必须带日志尾部（可观测失败）；
3. 旧进程未退出时 ``apply_now`` **中止**而不强停服务（RC_OLD_PID_ALIVE）；
4. ``cleanup_staging`` 保留最近一份 ``apply-*.log``（秒退根因取证）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory_agent.tasks import update as upd


@pytest.mark.skipif(os.name != "nt", reason="ctypes 判活路径仅 Windows")
def test_process_alive_sees_own_pid_without_win32api(monkeypatch) -> None:
    """win32api 缺失（打包遗漏）时判活必须仍可用，不得一律判死。"""

    import sys

    saved = {name: sys.modules.get(name) for name in ("win32api", "win32event", "win32process")}
    for name in saved:
        sys.modules[name] = None  # import 即失败，模拟打包遗漏
    try:
        assert upd._process_alive(os.getpid()) is True
        assert upd._process_alive(-1) is False
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_verify_helper_alive_reports_log_tail(tmp_path: Path) -> None:
    log = tmp_path / "apply-1.log"
    log.write_text("helper: boom", encoding="utf-8")
    handle = SimpleNamespace(pid=1234, log_path=log)
    with pytest.raises(upd.UpdateFailed) as excinfo:
        upd.verify_helper_alive(handle, process_alive=lambda pid: False, grace_s=0.0)
    assert "boom" in str(excinfo.value)


def test_verify_helper_alive_passes_when_alive() -> None:
    handle = SimpleNamespace(pid=1, log_path=Path("nowhere.log"))
    upd.verify_helper_alive(handle, process_alive=lambda pid: True, grace_s=0.05, sleep=lambda _s: None)


def test_apply_now_aborts_when_old_pid_alive(tmp_path: Path, monkeypatch) -> None:
    """旧 agent 未退出 → 返回 RC_OLD_PID_ALIVE 且绝不调用 service.stop()。"""

    target = tmp_path / "install"
    target.mkdir()
    (target / upd.INSTALL_ROOT_MARKER).write_text("", encoding="ascii")
    source = tmp_path / "payload"
    source.mkdir()

    stops: list[str] = []
    service = SimpleNamespace(
        wait_for_process_exit=lambda pid, timeout_s: False,
        stop=lambda: stops.append("stop") or True,
        start=lambda: True,
    )
    monkeypatch.setattr(upd, "plan_swap", lambda **_kw: SimpleNamespace(
        install_dir=target, payload_root=source,
        backup_dir=tmp_path / "b", failed_dir=tmp_path / "f",
    ))
    monkeypatch.setattr(upd, "precheck", lambda *_a, **_kw: None)

    rc = upd.apply_now(source, target, wait_pid=999, version="9.9.9", controller=service)
    assert rc == upd.RC_OLD_PID_ALIVE
    assert stops == [], "旧进程未退出不得强停服务"


def test_cleanup_staging_keeps_newest_helper_log(tmp_path: Path) -> None:
    logs = [tmp_path / f"apply-{i}.log" for i in range(3)]
    for i, path in enumerate(logs):
        path.write_text(str(i), encoding="utf-8")
        stamp = time.time() + i  # 旧的在前
        os.utime(path, (stamp, stamp))
    upd.cleanup_staging(tmp_path)
    assert logs[2].is_file(), "最近一份 helper 日志必须保留（秒退取证）"
    assert not logs[0].is_file() and not logs[1].is_file()


def test_config_oserror_reads_bak_without_overwriting(tmp_path, monkeypatch, caplog) -> None:
    """config 读取 OSError：用 .bak 内容兜底，但不得覆盖原文件（可能只是瞬时锁）。"""

    import json
    import logging

    from observatory_agent.core import config as cfg

    target = tmp_path / "config.json"
    bak = tmp_path / "config.json.bak"
    target.write_text(" garbage that never gets read ", encoding="utf-8")
    bak.write_text(json.dumps({"token": "tok-1", "device_uid": "OBS-X"}), encoding="utf-8")

    original_read = Path.read_text

    def _boom(self, *a, **k):
        if self == target:
            raise OSError("locked")
        return original_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _boom)
    with caplog.at_level(logging.ERROR):
        raw = cfg._load_config_json(target)
    monkeypatch.setattr(Path, "read_text", original_read)
    assert raw["token"] == "tok-1", "OSError 必须从 .bak 兜底，不得静默清零"
    assert target.read_text(encoding="utf-8") == " garbage that never gets read ", "原文件不得被 .bak 覆盖"


def test_config_oserror_without_bak_falls_to_defaults(tmp_path, monkeypatch) -> None:
    from observatory_agent.core import config as cfg

    target = tmp_path / "config.json"
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("locked")))
    assert cfg._load_config_json(target) == {}
