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


# --------------------------------------------------------- watchdog installer ---
def _watchdog_block() -> str:
    text = _install_script()
    return text.split("# ----------------------------------------------------------------- watchdog --", 1)[1].split(
        "function Uninstall-Agent", 1
    )[0]


def _watchdog_cmd(text: str) -> str:
    """从 here-string 里取出 .cmd 正文（含换行，便于与独立脚本比对）。"""

    return text.split("return @'\n", 1)[1].split("\n'@", 1)[0]


def test_watchdog_task_is_system_boot_and_periodic() -> None:
    """真名、SYSTEM、开机触发 + 周期巡检：缺一个断联就救不回来。"""

    block = _watchdog_block()
    assert "'Observatory'" in block, "任务必须真名，绝不伪装成系统维护"
    assert "Observatory Agent Watchdog" not in block, "任务名要短，就叫 Observatory"
    assert "<BootTrigger>" in block, "无人登录的机房不能靠 LogonTrigger"
    assert "<Delay>PT30S</Delay>" in block
    assert "<Interval>PT2M</Interval>" in block
    assert "<UserId>S-1-5-18</UserId>" in block
    assert "<RunLevel>HighestAvailable</RunLevel>" in block
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in block
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in block
    assert "伪装" not in block.split("function Get-WatchdogCmdText", 1)[1].split("function Get-WatchdogTaskXml", 1)[0]


def test_watchdog_cmd_heals_start_type_before_starting() -> None:
    """Start=4 时 sc start 返回 1058；必须先 sc config start= auto 再拉起。"""

    cmd = _watchdog_cmd(_watchdog_block())
    assert "reg query" in cmd and "/v Start" in cmd
    assert 'if /i not "%START_TYPE%"=="0x2"' in cmd, "3(manual)/4(disabled) 都要治"
    assert "sc config %SVC% start= auto" in cmd
    assert "sc start %SVC%" in cmd
    assert cmd.index("sc config %SVC% start= auto") < cmd.index("sc start %SVC%"), "顺序不能反"
    assert '>>"%LOG%"' in cmd, "不能全静默，出错要留痕"
    assert ">>\"%LOG%\" echo" in cmd
    assert "1056" in cmd, "already-running 竞态要标注为可忽略"
    # 不吞权限、不碰 360：只用系统内置工具，且动作只在自有目录。
    for forbidden in ("360Safe", "WhiteList.dat", "Stop-Process", "reg add", "certutil", "powershell -enc"):
        assert forbidden not in cmd, forbidden


def test_watchdog_cmd_is_idempotent_and_quiet_when_healthy() -> None:
    cmd = _watchdog_cmd(_watchdog_block())
    # 正常状态（0x2 + RUNNING）两个动作都不执行 → 不写日志、不重复 StartService。
    assert 'if /i not "%START_TYPE%"=="0x2" (' in cmd
    assert "if errorlevel 1 (" in cmd
    assert cmd.count("sc config %SVC% start= auto") == 1, "配置自愈只出现一次"
    assert cmd.count("sc start %SVC%") == 1


def test_watchdog_uses_ascii_cmd_and_utf16_xml() -> None:
    block = _watchdog_block()
    assert "-Encoding ASCII" in block, "cmd 必须全 ASCII，避免 ANSI 解码出中文乱码"
    assert "-Encoding Unicode" in block, "schtasks /XML 要求 UTF-16"
    assert "[System.Security.SecurityElement]::Escape" in block, "路径需 XML 转义"


def test_watchdog_failure_never_breaks_main_install() -> None:
    """360 可能当场拦 schtasks：加固项失败不能把已装好的服务带成安装失败。"""

    block = _watchdog_block()
    installer = block.split("function Install-Watchdog", 1)[1].split("function Remove-Watchdog", 1)[0]
    assert "catch" in installer
    assert "服务本身不受影响" in installer
    assert "throw" not in installer, "Install-Watchdog 不得抛异常"


def test_watchdog_removed_before_service_stop_and_on_uninstall() -> None:
    """否则 2 分钟周期任务会在停服/删除服务窗口把服务又拉起来。"""

    text = _install_script()
    uninstall = text.split("function Uninstall-Agent", 1)[1].split("# -------------------------------------------------------------------- main --", 1)[0]
    assert "Remove-Watchdog" in uninstall
    # 注释里也提到 Stop-Existing，用带缩进的调用行定位真实调用点。
    assert uninstall.index("Remove-Watchdog") < uninstall.index("\n    Stop-Existing")
    main = text.split("# -------------------------------------------------------------------- main --", 1)[1]
    assert main.index("Remove-Watchdog") < main.index("Install-Files -PackagePath"), "重装前先摘旧任务"
    assert "Install-Watchdog" in main, "安装流程必须注册"


def test_watchdog_only_mode_for_existing_devices() -> None:
    """存量设备远程补装：不下载包、不注册服务、不改版本。"""

    text = _install_script()
    header = text.split("[CmdletBinding()]", 1)[1].split("$ErrorActionPreference", 1)[0]
    assert "$WatchdogOnly" in header
    elevation = text.split("function Request-Elevation", 1)[1].split("function Stop-Existing", 1)[0]
    assert "'-WatchdogOnly'" in elevation, "提权后不能丢，否则会走成完整安装"
    main = text.split("# -------------------------------------------------------------------- main --", 1)[1]
    # 只截提权完成之后的分支区（前面的提权判断里也有一句 if ($Uninstall)）。
    branch = main.split("if ($WatchdogOnly) {", 1)[1].split("\n}\n", 1)[0]
    assert "Install-Watchdog" in branch
    assert "exit 0" in branch
    for forbidden in ("Install-Files", "Install-Service", "Resolve-BaseUrl", "Get-PackageWithFailover"):
        assert forbidden not in branch, forbidden
    assert main.index("if ($WatchdogOnly) {") < main.index("if ($Uninstall) {\n    Write-Step '开始卸载'")


def test_standalone_watchdog_script_matches_installer() -> None:
    """两份脚本必须逐字一致，否则存量设备与新装机行为会漂移。"""

    standalone = (REPO_ROOT / "tools/install-watchdog.ps1").read_text(encoding="utf-8")
    block = _watchdog_block()
    cmd_installer = _watchdog_cmd(block)
    cmd_standalone = standalone.split("$WatchdogCmd = @'\n", 1)[1].split("\n'@", 1)[0]
    assert cmd_standalone == cmd_installer, "watchdog.cmd 正文两份必须完全相同"
    for marker in ("<BootTrigger>", "<Interval>PT2M</Interval>", "S-1-5-18", "HighestAvailable"):
        assert marker in standalone, marker
    assert "'Observatory'" in standalone
    assert "Observatory Agent Watchdog" not in standalone
    assert "-Remove" in standalone, "要留可回滚的删除入口"


def test_script_parameters_are_declared() -> None:
    """``$PSBoundParameters.ContainsKey(...)`` 依赖参数被真正声明。"""

    text = _install_script()
    header = text.split("[CmdletBinding()]", 1)[1].split("$ErrorActionPreference", 1)[0]
    for name in ("BaseUrls", "Package", "Uninstall", "Insecure", "Quiet", "NoStart", "EnrollCode", "WatchdogOnly"):
        assert f"${name}" in header, name


# --------------------------------------------------- 安装器 UX（窗口/等待/退出） ---
INSTALL_CMD = REPO_ROOT / "server/observatory/templates/install.cmd"
USB_CMD = REPO_ROOT / "usb-installer/Install-ObservatoryAgent.cmd"


def _cmd_text(path: Path) -> str:
    assert path.exists(), f"缺少 {path}"
    return path.read_text(encoding="utf-8")


def test_cmd_elevates_itself_once_without_waiting() -> None:
    """双击只看到一个窗口：.cmd 自举提权，非管理员实例立即退出（不带 -Wait）。"""

    for path in (INSTALL_CMD, USB_CMD):
        text = _cmd_text(path)
        assert "net session >nul 2>&1" in text, path
        assert "-Verb RunAs" in text, path
        assert "-ArgumentList 'ELEVATED'" in text, path
        elevated = next(line for line in text.splitlines() if "-Verb RunAs" in line)
        assert "-Wait" not in elevated, "提权实例必须异步启动，原窗口立刻退出"
        assert 'if /i "%~1"=="ELEVATED"' in text, "提权实例要跳过再次提权"


def test_cmd_success_path_has_no_pause() -> None:
    """成功即自动关窗；失败保留 10 秒倒计时，不再要求按任意键。"""

    for path in (INSTALL_CMD, USB_CMD):
        text = _cmd_text(path)
        assert not any(line.strip().lower() == "pause" for line in text.splitlines()), path
        assert "timeout /t 10" in text, f"{path} 失败时要留出读错误的时间"


def test_usb_wizard_asks_after_elevation() -> None:
    """USB 向导先提权再输入：密钥不经过命令行/临时文件转发。"""

    text = _cmd_text(USB_CMD)
    assert text.index("-Verb RunAs") < text.index("set /p BASE_URL="), "先提权再问服务器/密钥"


def test_usb_builder_generates_elevating_cmds() -> None:
    """U 盘包生成的三份 .cmd 与仓库内模板同构：提权、无 pause、失败倒计时。"""

    source = (REPO_ROOT / "tools/build_usb_package.py").read_text(encoding="utf-8")
    assert source.count("-ArgumentList 'ELEVATED'") == 3, "一键/在线/卸载三份都要自举提权"
    assert source.count("timeout /t 10") == 3
    assert "\npause\n" not in source, "生成的 .cmd 不得再要求按任意键"


def test_watchdog_registers_before_service_start() -> None:
    """慢步骤（schtasks 可被主防拖住）必须在「服务已启动」之前完成。"""

    text = _install_script()
    main = text.split("# -------------------------------------------------------------------- main --", 1)[1]
    flow = main.split("Remove-Watchdog\n    Stop-Existing", 1)[1]
    files = flow.index("Install-Files -PackagePath $packagePath")
    watchdog = flow.index("Install-Watchdog")
    service = flow.index("Install-Service")
    assert files < watchdog < service, "顺序必须是 装文件 → 注册守护 → 启服务"


def test_install_watchdog_registration_is_bounded() -> None:
    """schtasks 被安全软件拦着时不会自己超时：注册必须有界且给进度。"""

    block = _watchdog_block()
    installer = block.split("function Install-Watchdog", 1)[1].split("function Remove-Watchdog", 1)[0]
    assert "正在注册服务守护任务" in installer, "慢步骤要有进度输出"
    assert "Wait-Process" in installer and "-Timeout 20" in installer, "不能无界等待"
    assert "& schtasks.exe /Create" not in installer, "老实现会无限期阻塞"
    assert "超时" in installer, "超时要明确告知（只警告，不 kill）"


def test_device_uid_poll_does_not_stall_install() -> None:
    """device_uid 只用于成功横幅：最多等 1 秒，取不到就提示去控制台看。"""

    text = _install_script()
    main = text.split("# -------------------------------------------------------------------- main --", 1)[1]
    uid_block = main.split("$deviceUid = ''", 1)[1]
    assert "for ($i = 0; $i -lt 5; $i++)" in uid_block
    assert "Milliseconds 200" in uid_block
    assert "稍后在控制台查看" in main


def test_remote_elevation_forwards_parameters() -> None:
    """``irm | iex`` 提权分支以前丢全部参数：准入密钥丢失即注册失败。"""

    text = _install_script()
    elevation = text.split("function Request-Elevation", 1)[1].split("function Stop-Existing", 1)[0]
    remote = elevation.split("# 提权子进程从服务器重新下载脚本", 1)[1]
    assert "[scriptblock]::Create" in remote, "下载后要带参数调用脚本块"
    assert "-EnrollCode '{0}'" in remote
    assert "-BaseUrls '{0}'" in remote
    for flag in ("-Insecure", "-Quiet", "-NoStart", "-Offline", "-Online", "-Uninstall", "-WatchdogOnly"):
        assert f"'{flag}'" in remote, flag


def test_agent_invocations_are_bounded() -> None:
    """360 极智守护会挂起无签名 exe 云查杀：同步无界等待 = 安装永久卡死（0.3.26 真机）。"""

    text = _install_script()
    assert "& $exe" not in text, "受控端 exe 调用必须全部走 Invoke-AgentBounded"
    helper = text.split("function Invoke-AgentBounded", 1)[1].split("function Install-Service", 1)[0]
    assert "Wait-Process" in helper and "-Timeout $TimeoutSeconds" in helper, "有界等待"
    install_service = text.split("function Install-Service", 1)[1].split(
        "# ----------------------------------------------------------------- watchdog --", 1
    )[0]
    assert "-TimeoutSeconds 60" in install_service, "--install 必须有界"
    assert "允许运行" in install_service, "超时报错要指向杀软放行，不能让现场猜"


def test_watchdog_success_is_verified_by_query() -> None:
    """schtasks 偶发打印「成功」却返回非 0 退出码（0.3.26 真机）：成败以 /Query 实证为准。"""

    block = _watchdog_block()
    installer = block.split("function Install-Watchdog", 1)[1].split("function Remove-Watchdog", 1)[0]
    assert "& schtasks.exe /Query /TN" in installer
    assert installer.index("schtasks.exe /Query") < installer.index("看门狗任务注册失败"), "查询结果优先于退出码"


def test_banner_warns_about_antivirus_prompts() -> None:
    """360 拦截表现为安装失败/卡死，现场没法归因：开装前就提示允许弹窗。"""

    text = _install_script()
    banner = text.split("function Show-Banner", 1)[1].split("function Test-Admin", 1)[0]
    assert "允许运行" in banner
