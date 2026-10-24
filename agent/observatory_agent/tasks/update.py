"""唯一更新路径：``verify sha256 → stage → precheck → swap → rollback``。

为什么旧实现错
--------------

* **A3（受控端侧）**：0.2.10 的远程 ``update`` 命令只带 ``version``（旧 ``runner.py:578-581``），
  于是 ``_maybe_update`` 传给 ``download_package`` 的 ``sha256`` 是空串，而
  ``client.download_package`` 的空值分支直接跳过校验（旧 ``client.py:234``）；
  同时 ``_forced`` 跳过 ``is_newer``，强制更新可以装旧版本。0.3.0 只有一条路径
  :func:`apply_update`：**空/非法 sha256 先被拒绝**，自动更新与强制更新共用，
  不存在绕过校验的分支。
* **A1 的更新侧**：旧 ``spawn_apply_helper`` 的整目录拷贝思路是对的（onedir 不能只搬运
  exe），但它把 helper 的 stderr 指到 DEVNULL，helper 秒退时只剩
  ``verify_helper_alive`` 一句“退出码”。新实现把 helper 的 stdout/stderr 落到
  ``staging/apply-*.log``，失败时把日志尾部带进异常信息（C6：可观测失败）。
* **重复的 wait/stop**：旧的 ``updater.py`` 与 ``uninstall.py`` 各有一份
  ``_wait_for_process_exit``/``_stop_service``；这里统一调用 :class:`win.service.ServiceController`
  （规格 §4.2 “win/service.py 一份”）。

时间全部通过参数注入（``now_ms``/``sleep``/``poll``），便于单测。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..core.commands import ERROR_BAD_REQUEST, Command, CommandHandler, CommandResult
from ..win.service import ServiceController, ServiceError

log = logging.getLogger(__name__)

EXE_NAME = "observatory-agent.exe"
VERSION_FILE = "VERSION"

#: 更新 helper 等待旧进程退出的上限（秒）。
SWAP_WAIT_S = 120.0
#: 更新包必须是 64 位十六进制 SHA-256。
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
#: 解压上限：防止 zip 炸弹把磁盘写满（A1 的“可观测失败”）。
MAX_ARCHIVE_ENTRIES = 50_000
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
#: helper 秒退判定的观察窗口。
HELPER_GRACE_S = 2.5
#: 安装根目录必须包含的 exe（防止把 ``_internal`` 当安装目录换掉——0.2.10 的事故）。
INSTALL_ROOT_MARKER = EXE_NAME

#: helper 模式返回码（``apply_now`` 的 docstring 与 main.py 共用）。
RC_OK = 0
RC_PAYLOAD_MISSING = 2
RC_SWAP_FAILED = 3
RC_PRECHECK_FAILED = 4
RC_ROLLED_BACK = 5
RC_ROLLBACK_FAILED = 6
RC_TARGET_INVALID = 7
RC_INTERRUPTED = 8


class UpdateError(RuntimeError):
    """更新失败；``reason`` 是可直接上报给控制台的短句。"""

    def __init__(self, reason: str, *, step: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.step = step


class UpdateRejected(UpdateError):
    """请求本身不合法（典型：缺 sha256、包校验失败、目标布局不对）。"""


class UpdateFailed(UpdateError):
    """执行阶段失败（下载/解压/预检/起 helper）。"""


class SwapFailed(UpdateError):
    """目录替换或新版本启动失败；``rolled_back`` 表示是否已恢复到旧版本。"""

    def __init__(self, reason: str, *, rolled_back: bool) -> None:
        super().__init__(reason, step="swap")
        self.rolled_back = rolled_back


class PackageFetcher(Protocol):
    """下载回调：把指定版本的安装包写到 ``destination``（由 link 层实现）。"""

    async def __call__(self, destination: Path) -> None: ...


EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


# ---------------------------------------------------------------- helpers ----
def default_now_ms() -> int:
    return int(time.time() * 1000)


def validate_sha256(value: str | None) -> str:
    """校验并归一化 sha256；空/非法一律拒绝（A3 的核心断言）。"""

    text = str(value or "").strip().lower()
    if not text:
        raise UpdateRejected("更新请求缺少 sha256，已拒绝（A3）", step="validate_sha256")
    if not _SHA256_RE.match(text):
        raise UpdateRejected(f"sha256 非法（长度/字符不合法）: {text[:16]}…", step="validate_sha256")
    return text


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_package(path: Path, expected_sha256: str) -> str:
    """校验安装包摘要；不一致时**删除**坏包并抛 :class:`UpdateRejected`。"""

    expected = validate_sha256(expected_sha256)
    if not path.is_file():
        raise UpdateFailed(f"安装包不存在: {path}", step="verify_package")
    actual = sha256_file(path)
    if actual != expected:
        with contextlib.suppress(OSError):  # 坏包删除失败不改变拒绝结论
            path.unlink()
        raise UpdateRejected(
            f"安装包 SHA-256 不匹配：实际 {actual[:12]}… 期望 {expected[:12]}…",
            step="verify_package",
        )
    return actual


def _pid_alive(pid: int) -> bool:
    """0.3.1（F34d）：进程是否存活（psutil 优先，缺依赖时 ctypes/OpenProcess 兜底）。"""

    if pid <= 0:
        return False
    try:
        import psutil  # noqa: PLC0415 - 按需导入，缺依赖走 ctypes 路径
    except ImportError:
        pass
    else:
        try:
            return bool(psutil.pid_exists(int(pid)))
        except Exception:  # noqa: BLE001 - psutil 查询失败按不存活处理
            return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
        except OSError:
            return False
        return True
    try:
        import ctypes  # noqa: PLC0415

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        try:
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 探测失败按不存活处理（宁误删不误留）
        return False


def _live_helper_pids(directory: Path) -> list[int]:
    """0.3.1（F34d）：staging 里仍在运行的 update helper pid 列表。

    ``spawn_helper`` 会为每个 helper 写 ``apply-<ts>.pid``；pid 文件缺失/损坏按
    已退出处理（旧版本没有 pid 文件，不影响清理）。
    """

    live: list[int] = []
    for pid_file in sorted(directory.glob("apply-*.pid")):
        try:
            pid = int(pid_file.read_text(encoding="ascii", errors="ignore").strip())
        except (OSError, ValueError):
            continue
        if _pid_alive(pid):
            live.append(pid)
    return live


def cleanup_staging(staging_dir: Path, *, keep_archive: bool = False) -> None:
    """清理上一次更新留下的 payload/apply/part/zip/日志。

    0.3.1（F34d）：删除前检查 ``apply-*.pid``，仍有存活的 helper 时跳过
    ``payload-*``/``apply-*`` 目录（helper 正从 payload 复制、往安装目录 swap，
    半路删掉它的源/工作目录会让 swap 断在中间）；无存活 helper 时顺带清掉
    过期的 pid 文件。
    """

    directory = Path(staging_dir)
    if not directory.is_dir():
        return
    live_pids = _live_helper_pids(directory)
    if live_pids:
        log.warning(
            "staging 仍有存活的 update helper（pid=%s），本次跳过 payload/apply 目录清理",
            live_pids,
        )
    for path in directory.iterdir():
        try:
            if path.is_dir() and path.name.startswith(("payload-", "apply-")):
                if live_pids:
                    continue
                shutil.rmtree(path, ignore_errors=True)
            elif path.suffix == ".part":
                path.unlink(missing_ok=True)
            elif path.suffix == ".zip" and not keep_archive:
                path.unlink(missing_ok=True)
            elif path.name.endswith(".log") and path.name.startswith("apply-"):
                path.unlink(missing_ok=True)
            elif path.suffix == ".pid" and path.name.startswith("apply-") and not live_pids:
                path.unlink(missing_ok=True)
        except OSError as exc:  # 清理是 best-effort，但失败要留痕
            log.warning("could not clean staging entry %s: %s", path, exc)


def extract_payload(
    archive: Path,
    destination: Path,
    *,
    max_entries: int = MAX_ARCHIVE_ENTRIES,
    max_total_bytes: int = MAX_EXTRACTED_BYTES,
) -> Path:
    """解压安装包并返回真正含 exe 的目录。

    防御 zip 路径穿越与 zip 炸弹；解压前逐条校验，非法包整包拒绝。
    """

    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as archive_file:
        members = archive_file.infolist()
        if len(members) > max_entries:
            raise UpdateRejected(f"安装包条目过多（{len(members)} > {max_entries}）", step="extract_payload")
        total = sum(member.file_size for member in members)
        if total > max_total_bytes:
            raise UpdateRejected(f"安装包解压体积过大（{total} 字节）", step="extract_payload")
        for member in members:
            target = (root / member.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise UpdateRejected(f"安装包包含非法路径: {member.filename}", step="extract_payload") from exc
        archive_file.extractall(destination)

    candidates = [destination, *[item for item in destination.iterdir() if item.is_dir()]]
    for candidate in candidates:
        if any(candidate.glob("*" + os.sep + EXE_NAME)) or (candidate / EXE_NAME).is_file():
            return candidate
    raise UpdateRejected(f"安装包里找不到 {EXE_NAME}", step="extract_payload")


def precheck(
    payload_root: Path,
    version: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    timeout_s: float = 60.0,
) -> str:
    """启动新版本 exe 的 ``--version``，确认它真的能运行且自称目标版本。

    旧实现把这一步放在 helper 里做；0.3.0 在**停服务之前**再预检一次，坏包直接拒绝，
    服务保持运行（A1 的反面：更新失败不能把设备弄下线）。
    """

    exe = Path(payload_root) / EXE_NAME
    if not exe.is_file():
        raise UpdateRejected(f"payload 缺少 {EXE_NAME}", step="precheck")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = run(
            [str(exe), "--version"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateFailed(f"新版本无法执行: {exc}", step="precheck") from exc
    reported = (completed.stdout or b"").decode("utf-8", "replace").strip()
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise UpdateFailed(f"新版本退出码 {completed.returncode}（stderr={stderr}）", step="precheck")
    if version and reported != version:
        raise UpdateFailed(f"新版本自称 {reported!r}，期望 {version!r}", step="precheck")
    return reported


# ------------------------------------------------------------------ stage ----
async def stage_update(
    *,
    version: str,
    sha256: str,
    fetch: PackageFetcher,
    staging_dir: Path,
    now_ms: Callable[[], int] = default_now_ms,
    on_event: EventCallback | None = None,
    package_size: int | None = None,
) -> Path:
    """下载 → 校验 → 解压 → 写入 VERSION，返回 payload 目录。

    0.3.1（F34e）：下载前做磁盘预检——``free < 3×包大小`` 直接拒绝（下载 + 解压
    + 目录备份都要空间）。``size_bytes`` 由服务端 update_offer 携带；缺失时跳过
    比例预检，解压阶段仍有 :data:`MAX_EXTRACTED_BYTES` 上限兜底。
    """

    checksum = validate_sha256(sha256)
    if not version:
        raise UpdateRejected("更新请求缺少 version", step="stage_update")
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    cleanup_staging(staging)
    if package_size and int(package_size) > 0:
        try:
            free = shutil.disk_usage(staging).free
        except OSError as exc:  # 读不到容量不阻塞更新，由解压上限兜底
            log.warning("无法读取磁盘剩余空间，跳过更新空间预检：%s", exc)
        else:
            required = 3 * int(package_size)
            if free < required:
                raise UpdateRejected(
                    f"磁盘剩余空间不足：更新需要约 {required} 字节（3× 包大小），仅剩 {free}",
                    step="stage_update",
                )
    else:
        log.debug("update offer 没有 size_bytes，跳过下载前的磁盘空间预检")
    archive = staging / f"observatory-agent-{version}.zip"
    if on_event is not None:
        await on_event("update_downloading", {"version": version})
    await fetch(archive)
    verify_package(archive, checksum)
    if on_event is not None:
        await on_event("update_extracting", {"version": version})
    payload_root = extract_payload(archive, staging / f"payload-{version}")
    (payload_root / VERSION_FILE).write_text(version + "\n", encoding="utf-8")
    log.info("agent package %s staged at %s (%s bytes)", version, payload_root, archive.stat().st_size)
    return payload_root


# ------------------------------------------------------------------ swap -----
@dataclass(frozen=True)
class SwapPlan:
    install_dir: Path
    payload_root: Path
    backup_dir: Path
    failed_dir: Path


def plan_swap(
    *,
    install_dir: Path,
    payload_root: Path,
    timestamp_ms: int,
    same_volume: Callable[[Path, Path], bool] | None = None,
) -> SwapPlan:
    """规划目录替换；校验安装根与同卷约束（纯规划 + 可注入的卷判断）。

    跨卷 ``os.replace`` 不可用（把 ``%ProgramData%`` 放到另一个卷的环境会失败），
    因此在规划阶段就拒绝并给出明确原因，而不是等 swap 失败后回滚。
    """

    target = Path(install_dir)
    source = Path(payload_root)
    if not (target / INSTALL_ROOT_MARKER).is_file():
        raise UpdateRejected(
            f"{target} 不像安装根（缺少 {INSTALL_ROOT_MARKER}），拒绝替换",
            step="plan_swap",
        )
    if not source.is_dir():
        raise UpdateRejected(f"payload 目录不存在: {source}", step="plan_swap")
    checker = same_volume or _same_volume
    if not checker(target, source):
        raise UpdateRejected(
            "staging 与安装目录不在同一卷，目录替换不可用（请把 ProgramData 与安装目录放在同一卷）",
            step="plan_swap",
        )
    stamp = int(timestamp_ms)
    return SwapPlan(
        install_dir=target,
        payload_root=source,
        backup_dir=target.with_name(f"{target.name}.old-{stamp}"),
        failed_dir=target.with_name(f"{target.name}.failed-{stamp}"),
    )


def _same_volume(first: Path, second: Path) -> bool:
    try:
        return os.stat(first).st_dev == os.stat(second).st_dev
    except OSError as exc:  # 路径不存在时交给后续步骤报错
        log.debug("same-volume check failed (%s, %s): %s", first, second, exc)
        return False


def swap_directories(plan: SwapPlan) -> None:
    """把 ``install_dir`` 改名备份，再把 payload 改名到目标；失败尽力回滚。"""

    moved = False
    if plan.backup_dir.exists():
        shutil.rmtree(plan.backup_dir, ignore_errors=True)
    try:
        if plan.install_dir.exists():
            os.replace(plan.install_dir, plan.backup_dir)
            moved = True
        os.replace(plan.payload_root, plan.install_dir)
    except OSError as exc:
        rollback_ok = False
        if moved and not plan.install_dir.exists():
            try:
                os.replace(plan.backup_dir, plan.install_dir)
                rollback_ok = True
                log.warning("swap failed, previous build restored")
            except OSError:
                log.exception("swap failed and rollback failed as well")
        raise SwapFailed(f"目录替换失败: {exc}", rolled_back=rollback_ok) from exc
    log.info("swap complete: %s -> %s", plan.payload_root, plan.install_dir)


def _remove_tree_with_retries(path: Path, *, attempts: int = 5, sleep: Callable[[float], None] = time.sleep) -> bool:
    for attempt in range(1, attempts + 1):
        if not path.exists():
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        sleep(1.5 * attempt)
    log.warning("could not remove %s after %s attempts (will be cleaned on next update)", path, attempts)
    return False


# ----------------------------------------------------------------- helper ----
@dataclass(frozen=True)
class HelperHandle:
    pid: int
    executable: Path
    workdir: Path
    log_path: Path


def spawn_helper(
    payload_root: Path,
    *,
    install_dir: Path,
    staging_dir: Path,
    version: str,
    current_pid: int,
    now_ms: Callable[[], int] = default_now_ms,
    spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> HelperHandle:
    """把 payload 整目录复制到私有 staging 后启动 helper。

    复制而不是在 payload 里直接运行：swap 会 ``os.replace`` 掉 payload 目录，正在运行的
    helper 不能从被搬走的目录里加载 DLL。这是 onedir 更新唯一可靠的形式。
    """

    source = Path(payload_root)
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    workdir = staging / f"apply-{version}-{int(now_ms()) % 10_000_000:07d}"
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    try:
        shutil.copytree(source, workdir)
    except OSError as exc:
        raise UpdateFailed(f"复制 update helper 失败: {exc}", step="spawn_helper") from exc

    helper = workdir / EXE_NAME
    if not helper.is_file():
        candidates = sorted(workdir.glob("*.exe"))
        if not candidates:
            raise UpdateFailed(f"payload 里没有可执行文件: {workdir}", step="spawn_helper")
        helper = candidates[0]
    log_path = staging / f"{workdir.name}.log"
    arguments = [
        str(helper),
        "--update-helper",
        str(source),
        "--target",
        str(install_dir),
        "--pid",
        str(int(current_pid)),
        "--version-name",
        version,
    ]
    creation_flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    try:
        with open(log_path, "wb") as log_file:
            process = spawn(
                arguments,
                cwd=str(workdir),
                creationflags=creation_flags,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
    except OSError as exc:
        raise UpdateFailed(f"无法启动 update helper: {exc}", step="spawn_helper") from exc
    log.info("update helper started pid=%s for %s (log %s)", process.pid, version, log_path)
    # 0.3.1（F34d）：落 pid 文件，cleanup_staging 据此识别"仍在运行的 helper"，
    # 避免把正在做 swap 的 helper 的源/工作目录删掉。
    pid_path = staging / f"{workdir.name}.pid"
    try:
        pid_path.write_text(f"{int(process.pid)}\n", encoding="ascii")
    except OSError as exc:  # 写不进 pid 文件只影响下次清理的存活判断
        log.warning("could not write helper pid file %s: %s", pid_path, exc)
    return HelperHandle(pid=int(process.pid), executable=helper, workdir=workdir, log_path=log_path)


def verify_helper_alive(
    helper: HelperHandle,
    *,
    process_alive: Callable[[int], bool] | None = None,
    grace_s: float = HELPER_GRACE_S,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 0.15,
) -> None:
    """确认 helper 没有秒退；秒退时把日志尾部带进异常（可观测失败）。"""

    check = process_alive or _process_alive
    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        if not check(helper.pid):
            raise UpdateFailed(
                f"update helper 启动即退出（pid={helper.pid}，日志尾部：{_read_log_tail(helper.log_path)}）",
                step="verify_helper_alive",
            )
        sleep(poll_s)
    if not check(helper.pid):
        raise UpdateFailed(
            f"update helper 在确认窗口内退出（pid={helper.pid}，日志尾部：{_read_log_tail(helper.log_path)}）",
            step="verify_helper_alive",
        )


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        import win32api
        import win32event
        import win32process

        handle = win32api.OpenProcess(win32process.SYNCHRONIZE, False, pid)
    except Exception:  # noqa: BLE001 - 进程已退出/无权限都视为不存活
        return False
    try:
        return win32event.WaitForSingleObject(handle, 0) != win32event.WAIT_OBJECT_0
    finally:
        win32api.CloseHandle(handle)


def _read_log_tail(path: Path, *, limit: int = 800) -> str:
    try:
        data = path.read_bytes()[-limit:]
    except OSError as exc:  # 日志缺失不应掩盖真正的失败原因
        return f"<无法读取日志 {path}: {exc}>"
    return data.decode("utf-8", "replace").strip() or "<空>"


# ------------------------------------------------------------- orchestration -
async def apply_update(
    *,
    version: str,
    sha256: str,
    fetch: PackageFetcher,
    staging_dir: Path,
    install_dir: Path,
    current_pid: int | None = None,
    spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
    on_event: EventCallback | None = None,
    now_ms: Callable[[], int] = default_now_ms,
    package_size: int | None = None,
) -> HelperHandle:
    """**唯一**更新入口（自动更新与强制更新共用）。

    调用方拿到 :class:`HelperHandle` 后才可以请求服务停止；本函数在返回前已经完成：
    摘要校验 → 下载 → 解压 → 预检（新版本能跑且版本号正确）→ 启动 helper 并确认存活。
    任何一步失败都抛 :class:`UpdateError`，服务**保持运行**。
    """

    checksum = validate_sha256(sha256)
    payload_root = await stage_update(
        version=version,
        sha256=checksum,
        fetch=fetch,
        staging_dir=staging_dir,
        now_ms=now_ms,
        on_event=on_event,
        package_size=package_size,
    )
    if on_event is not None:
        await on_event("update_precheck", {"version": version})
    precheck(payload_root, version)
    helper = spawn_helper(
        payload_root,
        install_dir=install_dir,
        staging_dir=staging_dir,
        version=version,
        current_pid=os.getpid() if current_pid is None else current_pid,
        now_ms=now_ms,
        spawn=spawn,
    )
    verify_helper_alive(helper)
    if on_event is not None:
        await on_event("update_helper_started", {"version": version, "pid": helper.pid})
    return helper


# ---------------------------------------------------------------- helper 模式 -
def apply_now(
    payload_root: str | Path,
    target_dir: str | Path,
    wait_pid: int,
    version: str,
    *,
    controller: ServiceController | None = None,
    now_ms: Callable[[], int] = default_now_ms,
    sleep: Callable[[float], None] = time.sleep,
    swap_wait_s: float = SWAP_WAIT_S,
) -> int:
    """update helper 模式：等待旧服务退出 → 预检 → 换目录 → 起服务 → 失败回滚。

    返回码见 ``RC_*`` 常量。判断顺序刻意保持“预检先于停服”：坏包不会让设备下线。
    """

    source = Path(payload_root)
    target = Path(target_dir)
    service = controller or ServiceController()
    if not source.is_dir():
        log.error("payload directory missing: %s", source)
        return RC_PAYLOAD_MISSING
    try:
        plan = plan_swap(
            install_dir=target,
            payload_root=source,
            timestamp_ms=now_ms(),
        )
    except UpdateRejected as exc:
        log.error("refusing to swap: %s", exc.reason)
        return RC_TARGET_INVALID
    try:
        precheck(source, version)
    except UpdateError as exc:
        log.error("payload precheck failed, keeping current build: %s", exc.reason)
        return RC_PRECHECK_FAILED

    if not service.wait_for_process_exit(wait_pid, timeout_s=swap_wait_s):
        log.warning("old service pid %s still alive after %.0fs", wait_pid, swap_wait_s)
    try:
        if not service.stop():
            log.error("could not stop the service before swapping")
            return RC_SWAP_FAILED
    except ServiceError as exc:
        log.error("stop service failed: %s", exc)
        return RC_SWAP_FAILED

    try:
        swap_directories(plan)
    except SwapFailed as exc:
        log.error("swap failed (rolled_back=%s): %s", exc.rolled_back, exc.reason)
        with contextlib.suppress(ServiceError):  # 尽力把旧版本拉起来，失败留给 SCM recovery
            service.start()
        return RC_SWAP_FAILED if not exc.rolled_back else RC_ROLLED_BACK

    if not service.start():
        log.error("new build did not come up; rolling back")
        rollback_ok = _rollback(plan, service, sleep=sleep)
        return RC_ROLLED_BACK if rollback_ok else RC_ROLLBACK_FAILED

    _remove_tree_with_retries(plan.backup_dir, sleep=sleep)
    _cleanup_failed_dirs(target)  # 0.3.1（F34f）：成功后清掉历史失败目录，保留最近 1 个取证
    log.info("update to %s finished", version)
    return RC_OK


def _replace_with_retries(
    source: Path,
    destination: Path,
    *,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """0.3.1（F34c）：``os.replace`` 带重试（默认 3 次，每次间隔 1s）。

    新版本启动失败后的目录可能仍被杀毒扫描/残留句柄短暂占用，立即重试往往
    就能成功；最后一次仍失败才把 OSError 抛给调用方。
    """

    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if attempt >= attempts:
                raise
            log.warning(
                "os.replace %s -> %s failed (attempt %s/%s): %s",
                source,
                destination,
                attempt,
                attempts,
                exc,
            )
            sleep(1.0)


def _rollback(plan: SwapPlan, service: ServiceController, *, sleep: Callable[[float], None]) -> bool:
    # 0.3.1（F34c）：先尽力停一次服务——新版本启动失败后服务可能仍在运行并占着
    # 安装目录句柄，不停服 os.replace 大概率失败。
    try:
        if service.stop():
            log.info("service stopped before rollback")
        else:
            log.warning("service.stop() reported failure before rollback; continuing")
    except ServiceError as exc:
        log.warning("service.stop() raised before rollback; continuing: %s", exc)
    try:
        if plan.install_dir.exists():
            _replace_with_retries(plan.install_dir, plan.failed_dir, sleep=sleep)
        if plan.backup_dir.exists():
            _replace_with_retries(plan.backup_dir, plan.install_dir, sleep=sleep)
    except OSError:
        log.exception("rollback after failed start did not work")
        return False
    started = service.start()
    if started:
        log.warning("rolled back to the previous build; update abandoned")
    else:
        log.error("previous build did not start either; manual attention required")
    return started


def _cleanup_failed_dirs(install_dir: Path, *, keep: int = 1) -> None:
    """0.3.1（F34f）：更新成功后清理旧的 ``<install>.failed-*`` 目录（保留最近 ``keep`` 个取证）。

    目录名内嵌毫秒时间戳（见 :func:`plan_swap`），按名字排序即时间序。
    """

    prefix = f"{install_dir.name}.failed-"
    try:
        failed = sorted(item for item in install_dir.parent.glob(f"{prefix}*") if item.is_dir())
    except OSError as exc:  # 枚举失败不影响更新成功的结论
        log.warning("could not list failed dirs under %s: %s", install_dir.parent, exc)
        return
    for path in failed[: max(0, len(failed) - max(0, keep))]:
        shutil.rmtree(path, ignore_errors=True)
        log.info("removed old failed dir %s", path)


# ---------------------------------------------------------------- handlers ---
def make_update_handler(
    *,
    fetch: PackageFetcher,
    staging_dir: Path,
    install_dir: Path,
    request_shutdown: Callable[[str], None],
    on_event: EventCallback | None = None,
    on_finished: Callable[[], None] | None = None,
    spawn_task: Callable[[Awaitable[Any]], Any] | None = None,
    current_pid: int | None = None,
    now_ms: Callable[[], int] = default_now_ms,
) -> CommandHandler:
    """构造 ``update`` 命令处理器（自动更新与强制更新同一条路径）。

    处理器**立即**返回 ``started`` 回执，真正的下载/校验/替换在后台任务里跑：先回执后
    动服务，保证回执能经链路送出去；后台任务确认 helper 存活后再 ``request_shutdown``。
    缺 sha256 / 版本为空 → ``BAD_REQUEST`` 回执，绝不开始下载（A3）。

    0.3.1（F34a）：后台任务**任何**异常都归一成 ``update_failed`` 事件并上报；
    任务结束时（无论成败）调用 ``on_finished``——调用方据此复位 running 标志，
    不再依赖"异常一定会被事件路径覆盖"。
    """

    state = {"running": False}

    def handle(command: Command) -> CommandResult:
        version = str(command.payload.get("version") or "").strip()
        try:
            checksum = validate_sha256(str(command.payload.get("sha256") or ""))
        except UpdateRejected as exc:
            return CommandResult(
                ok=False,
                result={"version": version},
                error=exc.reason,
                error_code=ERROR_BAD_REQUEST,
            )
        if not version:
            return CommandResult(
                ok=False, result={}, error="更新命令缺少 version", error_code=ERROR_BAD_REQUEST
            )
        if state["running"]:
            return CommandResult(
                ok=False,
                result={"version": version},
                error="已有更新在进行中",
                error_code=ERROR_BAD_REQUEST,
            )
        state["running"] = True
        try:
            package_size = max(0, int(command.payload.get("size_bytes") or 0)) or None
        except (TypeError, ValueError):
            package_size = None

        async def run() -> None:
            try:
                if on_event is not None:
                    await on_event("update_started", {"version": version})
                helper = await apply_update(
                    version=version,
                    sha256=checksum,
                    fetch=fetch,
                    staging_dir=staging_dir,
                    install_dir=install_dir,
                    current_pid=current_pid,
                    on_event=on_event,
                    now_ms=now_ms,
                    package_size=package_size,
                )
                log.info("update helper confirmed pid=%s version=%s", helper.pid, version)
                if on_event is not None:
                    await on_event("update_ready", {"version": version, "pid": helper.pid})
                request_shutdown(f"update:{version}")
            except UpdateError as exc:
                log.error("update to %s failed: %s", version, exc.reason)
                if on_event is not None:
                    await on_event("update_failed", {"version": version, "error": exc.reason})
            except Exception as exc:  # noqa: BLE001 - 0.3.1（F34a）：逃逸异常必须归一上报，
                # 否则 asyncio task 静默死亡，调用方的 running 标志只能靠 on_finished 兜底。
                log.exception("update to %s failed with unexpected error", version)
                if on_event is not None:
                    await on_event(
                        "update_failed",
                        {"version": version, "error": f"{type(exc).__name__}: {exc}"},
                    )
            finally:
                state["running"] = False
                if on_finished is not None:
                    with contextlib.suppress(Exception):  # 复位回调失败不能再炸一次 task
                        on_finished()

        launcher = spawn_task or asyncio.create_task
        launcher(run())
        return CommandResult(ok=True, result={"started": True, "version": version})

    return handle


def register_handlers(dispatcher: Any, **kwargs: Any) -> None:
    """把 ``update`` 注册到 :class:`core.commands.CommandDispatcher`（兼容裸 handlers dict）。"""

    handler = make_update_handler(**kwargs)
    register = getattr(dispatcher, "register", None)
    if callable(register):
        register("update", handler)
    else:
        dispatcher.handlers["update"] = handler


__all__ = [
    "EXE_NAME",
    "HELPER_GRACE_S",
    "HelperHandle",
    "INSTALL_ROOT_MARKER",
    "MAX_ARCHIVE_ENTRIES",
    "MAX_EXTRACTED_BYTES",
    "PackageFetcher",
    "RC_INTERRUPTED",
    "RC_OK",
    "RC_PAYLOAD_MISSING",
    "RC_PRECHECK_FAILED",
    "RC_ROLLBACK_FAILED",
    "RC_ROLLED_BACK",
    "RC_SWAP_FAILED",
    "RC_TARGET_INVALID",
    "SWAP_WAIT_S",
    "SwapFailed",
    "SwapPlan",
    "UpdateError",
    "UpdateFailed",
    "UpdateRejected",
    "apply_now",
    "apply_update",
    "cleanup_staging",
    "default_now_ms",
    "extract_payload",
    "make_update_handler",
    "plan_swap",
    "precheck",
    "register_handlers",
    "sha256_file",
    "spawn_helper",
    "stage_update",
    "swap_directories",
    "validate_sha256",
    "verify_helper_alive",
    "verify_package",
]