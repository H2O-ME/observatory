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


def test_ack_http_delivery_only_acks_delivered() -> None:
    """HTTP watermark ack 只确认真正投递过的消息（F22 载体一致性回归）。"""

    from observatory.link.envelope import new_envelope
    from observatory.link.outbox import Outbox

    class _Settings:
        outbox_max_messages = 16

    from observatory.link.session import AgentSession

    session = AgentSession.__new__(AgentSession)
    from collections import deque

    session.http_delivered = deque(maxlen=512)
    session.outbox = Outbox(maxsize=16)
    e1 = new_envelope("ping", {"n": 1}, msg_id="m1", clock=None)
    e2 = new_envelope("ping", {"n": 2}, msg_id="m2", clock=None)
    e3 = new_envelope("ping", {"n": 3}, msg_id="m3", clock=None)
    session.outbox.put(e1)
    session.outbox.put(e2)
    session.outbox.put(e3)

    # 只投递了 m1/m2（m3 还没发出去），agent 对 m2 ack
    session.http_delivered.extend(["m1", "m2"])
    removed = session.ack_http_delivery("m2")
    assert removed == 2
    assert session.outbox.pending_count() == 1, "未投递的 m3 不得被水位弹出"

    # ack 一个从未投递的 id：什么都不确认（多投安全，少投才是事故）
    session.http_delivered.extend(["m3"])
    assert session.ack_http_delivery("nope") == 0
    assert session.outbox.pending_count() == 1
    assert session.ack_http_delivery("m3") == 1


def test_setup_logging_survives_unwritable_dir(tmp_path, monkeypatch) -> None:
    """2026-11-04 helper 事故：日志目录建不起来不得杀死进程（降级仅 stderr）。"""
    import logging as std_logging

    from observatory_agent.core import logging as agent_logging

    bad = tmp_path / "denied"

    def refuse(*_a, **_k):
        raise PermissionError(5, "拒绝访问", str(bad))

    monkeypatch.setattr(agent_logging, "ensure_dirs", refuse)
    monkeypatch.setattr(
        agent_logging, "LOG_DIR", bad, raising=False
    ) if hasattr(agent_logging, "LOG_DIR") else None
    import observatory_agent.core.config as cfg

    monkeypatch.setattr(cfg, "LOG_DIR", bad)
    root = std_logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    path = agent_logging.setup_logging("update")
    assert path is None  # 目录建不起来：无文件日志，但不抛异常
    handlers = [type(h).__name__ for h in root.handlers]
    assert "StreamHandler" in handlers  # 降级为 stderr 输出
    for h in list(root.handlers):
        root.removeHandler(h)


def test_main_helper_path_reports_traceback_instead_of_silence(monkeypatch, capsys) -> None:
    """helper 启动期任何异常都要把 traceback 写进被重定向的日志，返回 EX_SOFTWARE。"""
    from observatory_agent import main as agent_main

    def boom(*_a, **_k):
        raise RuntimeError("模拟启动期未预期异常")

    monkeypatch.setattr("observatory_agent.tasks.update.apply_now", boom)
    rc = agent_main.main(
        ["--update-helper", "C:/nonexistent", "--target", "C:/nonexistent", "--pid", "1"]
    )
    assert rc == 70
    assert "RuntimeError" in capsys.readouterr().err
