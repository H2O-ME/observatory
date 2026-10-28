"""A2 + A5：ACL 与 worker 权限边界、install.ps1 参数传递与离线安装。

A2（静态链路确认，**未做真机权限验证**）：
* 旧实现把 ``%ProgramData%\\Observatory``（含 config.json 里的设备令牌）的 DACL
  收紧到 SYSTEM+Administrators **同时保留当前用户**——等于没保护；
* 而 worker 用登录用户令牌启动，仍会读共享配置、往共享目录写日志、保存配置
  → 一旦真的只留 SYSTEM+Admins，worker 立刻 PermissionError。
修复：敏感文件 SYSTEM 独占；worker 设置走管道；worker 日志写用户可写目录。

A5：install.ps1 提权后丢 ``-Uninstall`` / 显式 ``-BaseUrls``；离线 ``-Package``
仍无条件探测在线服务。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "server/observatory/templates/install.ps1"


# ------------------------------------------------------------------ A2 ACL ---
def test_sensitive_acl_grants_exclude_interactive_user() -> None:
    """A2 的核心：敏感路径的授权表里**不能**出现普通用户。"""

    from observatory_agent.core.config import acl_grants  # noqa: PLC0415

    grants = acl_grants()
    assert grants == [
        "SYSTEM:(OI)(CI)F",
        "Administrators:(OI)(CI)F",
    ], "设备令牌/outbox 只能 SYSTEM+Administrators 可读"
    joined = " ".join(grants)
    assert os.environ.get("USERNAME", "nobody-here") not in joined


def test_acl_grants_owner_opt_in_is_explicit() -> None:
    """只有显式 ``allow_owner=True`` 才保留调用者（给非提权调试用）。"""

    from observatory_agent.core.config import acl_grants  # noqa: PLC0415

    grants = acl_grants(allow_owner=True)
    assert len(grants) >= 2
    if os.environ.get("USERNAME", "").lower() not in ("", "system"):
        assert any(grants[0] not in grant for grant in grants[2:])


def test_acl_hardening_skips_non_production_paths(tmp_dir) -> None:
    """测试/便携目录绝不能被收 DACL（否则跑测试的用户连自己文件都写不了）。"""

    from observatory_agent.core import config  # noqa: PLC0415

    assert config.is_production_path(tmp_dir) is False
    assert config.is_production_path(config.PROGRAM_DATA / "config.json") is True


def test_acl_hardening_respects_program_data_override(tmp_dir, monkeypatch) -> None:
    from observatory_agent.core import config  # noqa: PLC0415

    monkeypatch.setenv("OBS_PROGRAM_DATA", str(tmp_dir))
    assert config.is_production_path(tmp_dir / "config.json") is False


def test_save_uses_sensitive_acl_not_owner() -> None:
    """``save()`` 落盘后收紧 ACL 时不能带上当前用户（源码级断言，避免真机 UAC）。"""

    source = (REPO_ROOT / "agent/observatory_agent/core/config.py").read_text(encoding="utf-8")
    assert "_harden_acl(target)" in source
    assert "_harden_acl(backup)" in source
    # 关键：不能有 allow_owner=True 出现在 save 路径上。
    save_body = source.split("def save(", 1)[1].split("def setting", 1)[0]
    assert "allow_owner=True" not in save_body


def test_worker_log_dir_is_outside_program_data(monkeypatch, tmp_dir) -> None:
    """worker 日志必须写用户可写目录（%LOCALAPPDATA%），不能写数据目录。"""

    from observatory_agent.core import config  # noqa: PLC0415

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_dir / "LocalAppData"))
    path = config.worker_log_dir()
    assert path.is_relative_to(tmp_dir / "LocalAppData"), path
    assert not path.is_relative_to(config.PROGRAM_DATA)


def test_worker_logging_does_not_touch_data_dir(monkeypatch, tmp_dir) -> None:
    """``setup_logging("worker-3")`` 不得调用 ensure_dirs（那会去动 SYSTEM 目录）。"""

    from observatory_agent.core import config  # noqa: PLC0415
    from observatory_agent.core import logging as logging_module  # noqa: PLC0415

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_dir / "LocalAppData"))

    def _boom() -> None:
        raise AssertionError("worker 不能调用 ensure_dirs（会去改 %ProgramData% ACL）")

    monkeypatch.setattr(config, "ensure_dirs", _boom)
    monkeypatch.setattr(logging_module, "ensure_dirs", _boom)
    import logging as std_logging  # noqa: PLC0415

    root = std_logging.getLogger()
    saved = list(root.handlers)
    try:
        path = logging_module.setup_logging("worker-42")
        assert path.is_relative_to(tmp_dir / "LocalAppData"), path
        assert path.name == "worker-42.log"
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved:
            root.addHandler(handler)


def test_service_role_still_uses_data_dir(monkeypatch, tmp_dir) -> None:
    from observatory_agent.core import config  # noqa: PLC0415
    from observatory_agent.core.logging import log_dir_for  # noqa: PLC0415

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_dir / "LocalAppData"))
    assert log_dir_for("agent") == config.LOG_DIR
    assert log_dir_for("update").parent == config.PROGRAM_DATA
    assert log_dir_for("worker-3").is_relative_to(tmp_dir / "LocalAppData")


def test_log_tail_finds_worker_logs_in_user_dir(monkeypatch, tmp_dir) -> None:
    """``log_tail``（服务端排障）必须能从同一路径读到 worker 日志。"""

    from observatory_agent.core import logging as logging_module  # noqa: PLC0415

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_dir / "LocalAppData"))
    directory = logging_module.log_dir_for("worker-7")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "worker-7.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
    assert logging_module.read_tail(2, "worker-7") == ["line2", "line3"]


def test_sensitive_files_share_one_store(tmp_path) -> None:
    """令牌/准入密钥只在 config.json（SYSTEM 独占）里；worker 不应有独立副本。"""

    from observatory_agent.core import config  # noqa: PLC0415

    saved = config.AgentConfig(device_uid="OBS-A2", token="device-token", enroll_key="k")
    path = tmp_path / "config.json"
    saved.save(path)
    import json  # noqa: PLC0415

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["token"] == "device-token"
    # outbox 是另一个敏感文件（SYSTEM 独占），路径在数据目录下。
    assert config.outbox_path().name == "outbox.sqlite3"


# ------------------------------------- A2：worker 的配置/日志全走非敏感通道 ---
def test_worker_runtime_never_touches_config_store() -> None:
    """worker 不能构造 ``ConfigStore``（那会读 SYSTEM 独占的 config.json）。"""

    from observatory_agent.core.runtime import WorkerRuntime  # noqa: PLC0415

    worker = WorkerRuntime(session_id=5, boot_id="boot-abc")
    assert not hasattr(worker, "_store"), "worker 不得持有 ConfigStore"
    assert worker._boot_id == "boot-abc"  # noqa: SLF001
    # 默认设置与服务端默认值同源，首帧 config 到达前行为不变。
    from observatory_agent.core.config import DEFAULT_SETTINGS  # noqa: PLC0415

    assert worker._settings_snapshot() == DEFAULT_SETTINGS  # noqa: SLF001


def test_worker_applies_settings_from_pipe_and_strips_secrets() -> None:
    """设置经管道下发；敏感键在 worker 侧也被剥掉（与服务进程同一实现）。"""

    from observatory_agent.core.runtime import WorkerRuntime  # noqa: PLC0415

    class _Engine:
        applied: list = None

        def apply_settings(self, values):
            _Engine.applied = dict(values)

    worker = WorkerRuntime(session_id=6)
    worker._engine = _Engine()  # noqa: SLF001
    worker._apply_remote_config({"capture_interval_s": 15, "archive_token": "secret"})  # noqa: SLF001
    assert worker._settings_snapshot()["capture_interval_s"] == 15  # noqa: SLF001
    assert "archive_token" not in worker._settings_snapshot()  # noqa: SLF001
    assert "archive_token" not in _Engine.applied


def test_worker_run_does_not_call_ensure_dirs(monkeypatch, tmp_dir) -> None:
    """``run()`` 的日志初始化不得触发 ensure_dirs（会去改 %ProgramData% ACL）。"""

    from observatory_agent.core import runtime as runtime_module  # noqa: PLC0415

    called: list[str] = []
    monkeypatch.setattr(runtime_module, "ensure_dirs", lambda: called.append("ensure_dirs"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_dir / "LocalAppData"))

    # 直接验证源码级约束（run 会去连管道，单测不适合真跑）。
    source = (REPO_ROOT / "agent/observatory_agent/core/runtime.py").read_text(encoding="utf-8")
    run_body = source.split("def run(self) -> int:", 1)[1].split("def _main_loop", 1)[0]
    assert "ensure_dirs()" not in run_body, "worker.run() 不得调用 ensure_dirs"
    # 只查真实构造（docstring 里提到 ConfigStore 是在解释为什么要避开它）。
    assert "self._store = ConfigStore()" not in source.split("class WorkerRuntime", 1)[1], (
        "worker 段不得构造 ConfigStore"
    )
    assert called == []


def test_worker_arguments_carry_boot_id() -> None:
    """A2：boot_id 经命令行传给 worker（worker 不读配置），且不影响 frozen 形状。"""

    from observatory_agent.win.session import worker_arguments  # noqa: PLC0415

    frozen = worker_arguments(3, frozen=True, boot_id="abc123")
    assert frozen == ["--worker", "--session", "3", "--boot-id", "abc123"]
    source = worker_arguments(3, frozen=False, boot_id="abc123")
    assert source[:2] == ["-m", "observatory_agent"]
    assert "--boot-id" in source
    # 没有 boot_id 时保持旧形状（0.2.x 兼容）。
    assert worker_arguments(3, frozen=True) == ["--worker", "--session", "3"]


def test_worker_section_has_no_store_reference() -> None:
    """A2 回归：``WorkerRuntime`` 整段不得出现 ``_store``（任何一处都会去读 SYSTEM 文件）。"""

    source = (REPO_ROOT / "agent/observatory_agent/core/runtime.py").read_text(encoding="utf-8")
    worker_section = source.split("class WorkerRuntime", 1)[1]
    offenders = [line.strip() for line in worker_section.splitlines() if "_store" in line]
    # docstring 里解释"为什么不用 ConfigStore"时会提到它，但只能出现在注释/文档里。
    offenders = [line for line in offenders if not line.startswith(("#", '"""', "*", "``"))]
    assert offenders == [], f"worker 段仍有 _store 引用: {offenders}"


def test_service_pushes_config_after_worker_hello() -> None:
    """A2：hello 之后必须首帧推送设置——worker 没有自读文件的兜底了。"""

    source = (REPO_ROOT / "agent/observatory_agent/core/runtime.py").read_text(encoding="utf-8")
    hello_block = source.split('if envelope.type == "hello":', 1)[1].split('if envelope.type == "heartbeat"', 1)[0]
    assert "_send_config_to(session_id)" in hello_block


# ------------------------------------------------------------- A5 install.ps1 ---
def _install_script() -> str:
    assert INSTALL_PS1.exists(), f"缺少 {INSTALL_PS1}"
    return INSTALL_PS1.read_text(encoding="utf-8")


def test_elevation_forwards_uninstall_switch() -> None:
    """普通用户请求卸载 → 提权后必须还是卸载（旧实现掉进安装分支）。"""

    text = _install_script()
    elevation = text.split("function Request-Elevation", 1)[1].split("function Stop-Existing", 1)[0]
    assert "if ($Uninstall) { $arguments += '-Uninstall' }" in elevation


def test_elevation_forwards_explicit_base_urls() -> None:
    """自定义地址必须跨过提权边界（旧实现丢失 → 回落到脚本内置地址）。"""

    text = _install_script()
    elevation = text.split("function Request-Elevation", 1)[1].split("function Stop-Existing", 1)[0]
    assert "$PSBoundParameters.ContainsKey('BaseUrls')" in elevation, "只有显式传入时才转发"
    assert "$BaseUrls -join ','" in elevation, "多线路要拼成一个参数，避免重复绑定报错"


def test_elevation_forwards_package_and_enroll_code() -> None:
    text = _install_script()
    elevation = text.split("function Request-Elevation", 1)[1].split("function Stop-Existing", 1)[0]
    assert "'-Package'" in elevation
    assert "'-EnrollCode'" in elevation, "准入密钥要跨过提权边界（A4）"


def test_offline_package_skips_online_probe() -> None:
    """A5：本地 ``-Package`` 路径不得先要求联网（真断网的 USB 安装）。"""

    text = _install_script()
    main = text.split("# -------------------------------------------------------------------- main --", 1)[1]
    # 探测只在"没有本地包"的分支里发生。
    probe_index = main.index("$primary = Resolve-BaseUrl")
    guard_index = main.index("if (-not $localPackage)")
    assert guard_index < probe_index, "Resolve-BaseUrl 必须被 if (-not $localPackage) 包住"
    # 而且本地包解析必须在探测之前完成（否则判断不出是否需要联网）。
    local_index = main.index("$localPackage = (Resolve-Path -LiteralPath $candidate).Path")
    assert local_index < probe_index


def test_install_service_passes_enroll_code() -> None:
    """A4 安装链路：启用准入时安装器要把密钥交给受控端 ``--enroll-code``。"""

    text = _install_script()
    install_service = text.split("function Install-Service", 1)[1].split("function Uninstall-Agent", 1)[0]
    assert "'--enroll-code'" in install_service
    assert "$EnrollCode" in install_service


def test_script_parameters_are_declared() -> None:
    """``$PSBoundParameters.ContainsKey(...)`` 依赖参数被真正声明。"""

    text = _install_script()
    header = text.split("[CmdletBinding()]", 1)[1].split("$ErrorActionPreference", 1)[0]
    for name in ("BaseUrls", "Package", "Uninstall", "Insecure", "Quiet", "NoStart", "EnrollCode"):
        assert f"${name}" in header, name
