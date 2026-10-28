"""受控端入口与模式分发（0.3.0）。

一个可执行文件承担多个角色，这是"无 UAC 覆盖升级/卸载"的实现方式（更新助手与
卸载助手必须是被替换目录之外的另一个进程）：

``(no args)``             以 Windows 服务运行（由 SCM 启动；非 SCM 启动回退 console）
``--console``             前台运行（调试用，不过 SCM）
``--worker --session N``  用户会话内的采集/输入/文件 worker
``--update-helper ...``   分离的更新助手：替换安装目录
``--purge-helper ...``    分离的卸载助手：删除全部痕迹
``--version`` / ``--doctor``  版本与现场诊断

0.3.0 的纪律：
- 新代码只 import ``core/``、``win/``、``tasks/`` 三处；**不得** import 0.2.10 的
  旧模块（``runner``/``transport``/``client``/``worker``/``spool``/``config``/
  ``logging_setup`` 等），旧文件由 Lead 统一删除。
- ``--version`` 与 ``--doctor`` 不依赖 SCM 状态：现场排障时即使服务层坏了也必须能跑。
- 更新/卸载助手保持 0.2.10 的**命令行形状**（N2：0.2.x 受控端零重装升级，旧版
  updater 会用 ``--apply-update/--version-name`` 拉起新版本的 exe）；新形状
  （``--update-helper/--purge-helper``）与 agent-sys 的实现一致。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

#: 裸 ``--version`` 的哨兵值；``--version <x.y.z>`` 是 0.2.x 更新助手的兼容形式。
PRINT_VERSION = "\x00print-version"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="observatory-agent", description="Observatory 受控端 0.3.0")
    parser.add_argument(
        "--version",
        nargs="?",
        const=PRINT_VERSION,
        default="",
        help="显示版本号；也接受 ``--version <x.y.z>``（0.2.x 更新助手兼容形式）",
    )

    parser.add_argument("--console", action="store_true", help="前台运行（调试用）")
    parser.add_argument("--worker", action="store_true", help="以会话工作进程身份运行")
    parser.add_argument("--session", type=int, default=0, help="工作进程所属会话 ID")

    # -- 更新/卸载助手（新形状 + 0.2.10 兼容形状）------------------------
    parser.add_argument("--update-helper", dest="update_helper", default="", help="（内部）待应用的更新目录")
    parser.add_argument("--apply-update", dest="apply_update", default="", help="（兼容 0.2.10）待应用的更新目录")
    parser.add_argument("--target", default="", help="（内部）安装目录")
    parser.add_argument("--pid", type=int, default=0, help="（内部）等待退出的进程")
    parser.add_argument("--version-name", dest="version_name", default="", help="（内部）目标版本")
    parser.add_argument("--purge-helper", dest="purge_helper", action="store_true", help="（内部）卸载助手模式")
    parser.add_argument("--purge", action="store_true", help="（兼容 0.2.10）卸载助手模式")
    parser.add_argument("--wait-pid", dest="wait_pid", type=int, default=0)
    parser.add_argument("--install-dir", dest="install_dir", default="")
    parser.add_argument("--data-dir", dest="data_dir", default="")
    parser.add_argument("--confirm-file", dest="confirm_file", default="")

    # -- 运维命令 ---------------------------------------------------------
    parser.add_argument("--install", action="store_true", help="安装/更新服务注册（需要管理员）")
    parser.add_argument("--uninstall", action="store_true", help="本地卸载受控端（需要管理员）")
    parser.add_argument("--start", action="store_true", help="启动服务")
    parser.add_argument("--stop", action="store_true", help="停止服务")
    parser.add_argument("--status", action="store_true", help="查看服务状态")
    parser.add_argument("--doctor", action="store_true", help="打印诊断信息")
    # 兼容：旧安装脚本仍会传这两个参数。0.3.7 起注册开放，它们是空操作，
    # 保留只为让"老脚本 + 新二进制"能装完，不再写进配置（也不打印）。
    parser.add_argument("--enroll-code", dest="enroll_code", default="", help=argparse.SUPPRESS)
    parser.add_argument("--install-token", dest="install_token", default="", help=argparse.SUPPRESS)
    parser.add_argument("--base-url", dest="base_urls", action="append", default=[], help="服务器地址（可多次）")
    parser.add_argument("--exe", default="", help="注册服务时使用的可执行文件路径")
    return parser


# ------------------------------------------------------------- 模式分发 ---
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 0.2.x 的更新助手把 ``--version <目标版本>`` 当选项传；裸 ``--version`` 才是
    # "打印版本"。两种都要能跑（N2）。
    if args.version == PRINT_VERSION:
        print(__version__)
        return 0

    if args.update_helper or args.apply_update:
        return _run_update_helper(args)
    if args.purge_helper or args.purge:
        return _run_purge_helper(args)

    if args.worker:
        return _run_worker(args.session)

    if args.doctor:
        return _run_doctor()

    if args.install:
        return _install(args)
    if args.uninstall:
        return _uninstall()
    if args.start or args.stop or args.status:
        return _service_action(args)

    _apply_cli_config(args)
    if args.console:
        return _run_console()
    return _run_service()


# ---------------------------------------------------------------- 服务 ---
def _run_service() -> int:
    """SCM 模式；不是由 SCM 拉起（例如双击/前台执行）时回退到 console。"""

    from .core.logging import setup_logging
    from .core.runtime import create_service_app
    from .win import WindowsOnlyOperation
    from .win.service import NOT_STARTED_BY_SCM, ServiceError, run_service

    try:
        code = int(run_service(create_service_app, prepare=lambda: setup_logging("agent")))
    except WindowsOnlyOperation as exc:
        print(f"受控端只能在 Windows 上作为服务运行：{exc}", file=sys.stderr)
        return 1
    except ServiceError as exc:
        print(f"服务启动失败：{exc}", file=sys.stderr)
        return 1
    if code == NOT_STARTED_BY_SCM:
        log.info("非 SCM 启动，回退到前台模式")
        return _run_console()
    return code


def _run_console() -> int:
    from .core.runtime import run_console

    return int(run_console())


def _run_worker(session_id: int) -> int:
    """用户会话内的 worker；由服务进程用 CreateProcessAsUser 拉起。"""

    from .core.runtime import run_worker

    return int(run_worker(int(session_id)))


def _install(args: argparse.Namespace) -> int:
    from .core.config import AgentConfig, ConfigStore, ensure_dirs
    from .core.logging import setup_logging
    from .win import WindowsOnlyOperation
    from .win.service import ServiceController, ServiceError, install_service

    setup_logging("install", console=True)
    ensure_dirs()
    changes = _cli_config_changes(args)
    if changes:
        ConfigStore().update(**changes)
    executable = args.exe or sys.executable
    try:
        install_service(executable)
    except (ServiceError, WindowsOnlyOperation) as exc:
        print(f"安装服务失败：{exc}", file=sys.stderr)
        return 1
    started = False
    try:
        started = ServiceController().start()
    except ServiceError as exc:
        print(f"服务已注册但启动失败：{exc}", file=sys.stderr)
    config = AgentConfig.load()
    print(f"版本：{__version__}")
    print(f"设备标识：{config.device_uid}")
    print(f"服务器：{', '.join(config.base_urls)}")
    print(f"服务启动：{'成功' if started else '未确认（请查看服务状态）'}")
    return 0 if started else 1


def _uninstall() -> int:
    from .core.config import INSTALL_DIR
    from .core.logging import setup_logging
    from .tasks.uninstall import interactive_uninstall

    setup_logging("uninstall", console=True)
    # 返回 0 后 main 退出进程；helper 在等本进程消失后继续清理（A1）。
    return int(interactive_uninstall(install_dir=INSTALL_DIR))


def _service_action(args: argparse.Namespace) -> int:
    from .win import WindowsOnlyOperation
    from .win.service import SERVICE_RUNNING, SERVICE_STOPPED, ServiceController, ServiceError

    controller = ServiceController()
    try:
        if args.start:
            return 0 if controller.start() else 1
        if args.stop:
            return 0 if controller.stop() else 1
        status = controller.status()
    except (ServiceError, WindowsOnlyOperation) as exc:
        print(f"服务操作失败：{exc}", file=sys.stderr)
        return 1
    names = {SERVICE_STOPPED: "已停止", SERVICE_RUNNING: "运行中"}
    print(f"版本：{__version__}")
    print(f"服务状态：{names.get(status.state, status.state)}")
    print(f"服务 PID：{status.pid}")
    return 0


def _apply_cli_config(args: argparse.Namespace) -> None:
    changes = _cli_config_changes(args)
    if not changes:
        return
    from .core.config import ConfigStore, ensure_dirs
    from .core.logging import setup_logging

    ensure_dirs()
    setup_logging("agent", console=True)
    ConfigStore().update(**changes)
    log.info("命令行参数已写入配置：%s", sorted(changes))


def _cli_config_changes(args: argparse.Namespace) -> dict:
    changes: dict = {}
    # --enroll-code / --install-token 已废弃：注册不需要凭据，旧脚本传了也不写配置。
    if args.base_urls:
        changes["base_urls"] = [str(url).rstrip("/") for url in args.base_urls if str(url).strip()]
    return changes


# ---------------------------------------------------------- 更新/卸载助手 ---
def _run_update_helper(args: argparse.Namespace) -> int:
    """更新助手：校验→替换→回滚都在 ``tasks/update.py``，这里只转发参数。

    兼容性：0.2.x 的 updater 传 ``--apply-update <payload> --target <install>
    --pid <pid> --version-name <ver>``；0.3.0 的新形状是 ``--update-helper``。
    """

    from .core.logging import setup_logging
    from .tasks.update import apply_now

    setup_logging("update")
    payload_root = args.update_helper or args.apply_update
    version = args.version_name or ("" if args.version == PRINT_VERSION else args.version)
    return int(apply_now(payload_root, args.target, args.pid, version))


def _run_purge_helper(args: argparse.Namespace) -> int:
    """卸载助手：只在 helper 确认存活并写回 ok=true 后才让服务端 ack（A1）。"""

    from .core.config import INSTALL_DIR, program_data_dir
    from .core.logging import setup_logging
    from .tasks.uninstall import run_purge_helper

    setup_logging("uninstall")
    data_dir = Path(args.data_dir) if args.data_dir else program_data_dir()
    confirm_file = Path(args.confirm_file) if args.confirm_file else data_dir / "uninstall-status.json"
    return int(
        run_purge_helper(
            wait_pid=int(args.wait_pid or 0),
            install_dir=args.install_dir or str(INSTALL_DIR),
            data_dir=data_dir,
            confirm_file=confirm_file,
        )
    )


# ---------------------------------------------------------------- 诊断 ---
def _run_doctor() -> int:
    """现场诊断：只读配置与目录，不联网、不启动服务。"""

    from .core.config import (
        CONFIG_PATH,
        INSTALL_DIR,
        LOG_DIR,
        PROGRAM_DATA,
        STAGING_DIR,
        AgentConfig,
        outbox_limits_from_env,
    )

    config = AgentConfig.load()
    limits = outbox_limits_from_env()
    print(f"版本            : {__version__}")
    print(f"可执行文件      : {sys.executable}")
    print(f"安装目录        : {INSTALL_DIR}")
    print(f"数据目录        : {PROGRAM_DATA} (存在={PROGRAM_DATA.exists()})")
    print(f"日志目录        : {LOG_DIR}")
    print(f"暂存目录        : {STAGING_DIR}")
    print(f"配置文件        : {CONFIG_PATH} (存在={CONFIG_PATH.exists()})")
    print(f"设备标识        : {config.device_uid}")
    print(f"本次 boot_id    : {config.boot_id}")
    print(f"已注册          : {'是' if config.token else '否'}")
    print(f"服务器地址      : {', '.join(config.base_urls)}")
    print(f"心跳间隔        : {config.setting('heartbeat_interval_s', 20)}s")
    print(f"outbox 上限     : {limits.max_items} 条 / {limits.max_bytes // (1024 * 1024)} MiB（{limits.overflow}）")
    print(f"服务状态        : {_safe_service_status()}")
    return 0


def _safe_service_status() -> str:
    try:
        from .win.service import ServiceController
    except ImportError as exc:  # pragma: no cover - 打包缺模块时诊断仍要能跑
        return f"不可用（{exc}）"
    try:
        status = ServiceController().status()
    except Exception as exc:  # noqa: BLE001 - 诊断命令本身不能因为服务层异常而失败
        log.debug("读取服务状态失败", exc_info=True)
        return f"读取失败: {exc}"
    return f"state={status.state} pid={status.pid}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))