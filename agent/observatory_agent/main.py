"""Agent entry point and mode dispatcher.

One executable, several roles — that is what makes the update/uninstall helpers
possible without shipping extra binaries:

``(no args)``             run as the Windows service (or console when not started by the SCM)
``--console``             run the network runner in the foreground, no service needed
``--worker --session N``  capture worker inside session N
``--install``             register the service (the only operation needing elevation)
``--apply-update ...``    detached helper that swaps the install directory
``--purge ...``           detached helper that removes every trace
``--status`` / ``--doctor``  print diagnostics
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

#: Sentinel for the bare ``--version`` flag (no value supplied).
PRINT_VERSION = "\x00print-version"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="observatory-agent", description="Observatory 受控端")
    # ``--version`` doubles as the bare "print version" flag and, historically, as
    # the helper's target-version option: ``updater.spawn_apply_helper`` passed
    # ``--version 0.2.1``. argparse rejected that value, so every update helper
    # exited before doing anything and left the service stopped. Accepting an
    # optional value keeps already-installed 0.2.0 agents upgradeable.
    parser.add_argument(
        "--version",
        nargs="?",
        const=PRINT_VERSION,
        default="",
        help="显示版本号；也接受 ``--version <x.y.z>``（更新助手兼容形式）",
    )

    parser.add_argument("--console", action="store_true", help="前台运行（调试用）")
    parser.add_argument("--worker", action="store_true", help="以会话工作进程身份运行")
    parser.add_argument("--session", type=int, default=0, help="工作进程所属会话 ID")

    parser.add_argument("--apply-update", dest="apply_update", default="", help="（内部）应用更新目录")
    parser.add_argument("--target", default="", help="（内部）安装目录")
    parser.add_argument("--pid", type=int, default=0, help="（内部）等待退出的进程")
    parser.add_argument("--version-name", dest="version_name", default="", help="（内部）目标版本")

    parser.add_argument("--purge", action="store_true", help="（内部）静默卸载")
    parser.add_argument("--wait-pid", dest="wait_pid", type=int, default=0)
    parser.add_argument("--install-dir", dest="install_dir", default="")

    parser.add_argument("--install", action="store_true", help="安装/更新服务注册（需要管理员）")
    parser.add_argument("--uninstall", action="store_true", help="卸载受控端（需要管理员）")
    parser.add_argument("--start", action="store_true", help="启动服务")
    parser.add_argument("--stop", action="store_true", help="停止服务")
    parser.add_argument("--status", action="store_true", help="查看服务状态")
    parser.add_argument("--doctor", action="store_true", help="打印诊断信息")
    parser.add_argument("--enroll-code", dest="enroll_code", default="", help="注册码")
    parser.add_argument("--base-url", dest="base_urls", action="append", default=[], help="服务器地址（可多次）")
    parser.add_argument("--exe", default="", help="注册服务时使用的可执行文件路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version == PRINT_VERSION:
        print(__version__)
        return 0

    if args.worker:
        from .worker import main as worker_main

        return worker_main(args.session)

    if args.apply_update:
        from .updater import apply_now

        # Prefer the explicit ``--version-name``; fall back to the value passed
        # through ``--version`` by older updaters.
        target_version = args.version_name or (args.version if args.version != PRINT_VERSION else "")
        return apply_now(args.apply_update, args.target, args.pid, target_version)

    if args.purge:
        from .uninstall import purge

        return purge(args.wait_pid, args.install_dir or None)

    if args.install:
        return _install(args)

    if args.uninstall:
        from .uninstall import interactive_uninstall

        return interactive_uninstall()

    if args.start or args.stop or args.status:
        return _service_action(args)

    if args.doctor:
        return _doctor()

    if args.console:
        # Console mode honours the same overrides as the installer so a developer
        # can point a checkout at a local server without touching the real
        # %ProgramData%\Observatory enrolment.
        _apply_cli_config(args)
        return run_console()

    from .service import run_service

    return run_service()


def _cli_config_changes(args: argparse.Namespace) -> dict:
    changes: dict = {}
    if args.enroll_code:
        changes["enroll_code"] = args.enroll_code
    if args.base_urls:
        changes["base_urls"] = [url.rstrip("/") for url in args.base_urls if url.strip()]
    return changes


def _apply_cli_config(args: argparse.Namespace) -> None:
    from .config import ConfigStore, ensure_dirs

    changes = _cli_config_changes(args)
    if not changes:
        return
    ensure_dirs()
    ConfigStore().update(**changes)


def _install(args: argparse.Namespace) -> int:
    from .config import AgentConfig, ConfigStore, ensure_dirs
    from .service import install_service, is_installed, start_service

    ensure_dirs()
    store = ConfigStore()
    changes = _cli_config_changes(args)
    if changes:
        store.update(**changes)

    executable = args.exe or sys.executable
    code = install_service(executable)
    if code != 0:
        return code
    if is_installed():
        start_service()
    config = AgentConfig.load()
    print(f"版本：{__version__}")
    print(f"设备标识：{config.device_uid}")
    print(f"服务器：{', '.join(config.base_urls)}")
    return 0


def _service_action(args: argparse.Namespace) -> int:
    from .service import service_status, start_service, stop_service

    if args.start:
        return start_service()
    if args.stop:
        return stop_service()
    status = service_status()
    state = status.get("state")
    names = {1: "已停止", 2: "启动中", 3: "启动中", 4: "运行中", 5: "停止中", 6: "停止中", 7: "已暂停"}
    print(f"版本：{__version__}")
    print(f"服务状态：{names.get(state, state)} {status.get('error', '')}".strip())
    print(f"服务 PID：{status.get('pid', 0)}")
    return 0


def run_console() -> int:
    from .config import ensure_dirs
    from .logging_setup import setup_logging
    from .runner import AgentRunner

    ensure_dirs()
    setup_logging("agent", console=True)
    log.info("console mode, version %s", __version__)
    runner = AgentRunner(service_mode=False)
    runner.on_shutdown = lambda reason: log.info("shutdown requested: %s", reason)
    runner.start()
    try:
        while True:
            time.sleep(1)
            if not runner._thread or not runner._thread.is_alive():  # noqa: SLF001 - console convenience
                break
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        runner.stop()
    return 0


def _doctor() -> int:
    from .config import AgentConfig, CONFIG_PATH, INSTALL_DIR, LOG_DIR, PROGRAM_DATA
    from .client import BaseRouter
    from .service import service_status

    config = AgentConfig.load()
    print(f"版本            : {__version__}")
    print(f"可执行文件      : {sys.executable}")
    print(f"安装目录        : {INSTALL_DIR}")
    print(f"数据目录        : {PROGRAM_DATA} (存在={PROGRAM_DATA.exists()})")
    print(f"日志目录        : {LOG_DIR}")
    print(f"配置文件        : {CONFIG_PATH} (存在={CONFIG_PATH.exists()})")
    print(f"设备标识        : {config.device_uid}")
    print(f"已注册          : {'是' if config.token else '否'}")
    print(f"服务器地址      : {', '.join(config.base_urls)}")
    print(f"服务状态        : {service_status()}")
    print(f"当前传输级别    : {'ws' if config.setting('ws_enabled', True) else 'poll'}")

    import asyncio

    async def probe() -> None:
        import httpx

        router = BaseRouter(config.base_urls)
        for url in router.urls:
            started = time.time()
            try:
                async with httpx.AsyncClient(timeout=8, verify=False) as http:
                    response = await http.get(f"{url}/api/v1/health")
                elapsed = int((time.time() - started) * 1000)
                body = response.json() if response.status_code == 200 else {}
                print(f"  {url:<40} {response.status_code} {elapsed}ms 版本={body.get('version', '?')}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {url:<40} 不可达: {exc}")

    print("连通性：")
    try:
        asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001
        print(f"  探测失败: {exc}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
_ = Path