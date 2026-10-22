"""Auto-update: install once, then update in place.

Sequence (no elevation at any point — the agent runs as SYSTEM already):

1. Server announces ``version`` + ``sha256`` on the heartbeat.
2. The running service downloads the zip, verifies the digest, unpacks it into
   ``%ProgramData%\\Observatory\\staging\\payload-<version>``.
3. A copy of the current executable is started as a *detached* helper with
   ``--apply-update``; the service then stops itself.
4. The helper waits for the old process to exit, swaps the install directory and
   starts the service again.

The old directory is renamed rather than deleted first so a failed swap can be
rolled back by the next start.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import INSTALL_DIR, STAGING_DIR

log = logging.getLogger(__name__)

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
SWAP_WAIT_S = 120
#: Name of the executable inside both the install directory and a staged payload.
EXE_NAME = "observatory-agent.exe"
#: How many times the helper tries to bring the service back up after the swap.
SERVICE_START_ATTEMPTS = 6
#: Linear backoff between start attempts (seconds); multiplied by the attempt number.
SERVICE_START_BACKOFF_S = 3.0
#: How long one start attempt may stay in START_PENDING before it counts as failed.
SERVICE_START_SETTLE_S = 25.0
#: After the service reports RUNNING, how long to wait before re-checking it is
#: still alive (catches builds that crash immediately after start-up).
SERVICE_CONFIRM_S = 3.0


def verify_payload(payload_root: Path, version: str) -> tuple[bool, str]:
    """Run the staged executable once to prove it can actually start.

    This is deliberately done *before* the running service is stopped. A payload
    that cannot even print its version (wrong Python runtime, missing
    ``_internal``, antivirus quarantine, corrupt download) would otherwise cost
    the machine its service: the old build is already gone and nothing on the box
    restarts it. Validating first turns that failure mode into "update skipped,
    keep running the current version".
    """

    exe = Path(payload_root) / EXE_NAME
    if not exe.is_file():
        return False, f"payload 缺少 {EXE_NAME}"
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no user input
            [str(exe), "--version"],
            capture_output=True,
            timeout=60,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"payload 无法执行: {exc}"
    reported = (completed.stdout or b"").decode("utf-8", "replace").strip()
    if completed.returncode != 0:
        return False, f"payload 退出码 {completed.returncode} (stderr={(completed.stderr or b'').decode('utf-8', 'replace').strip()[:200]})"
    if version and reported != version:
        return False, f"payload 报告版本 {reported!r}，期望 {version!r}"
    return True, reported


async def apply_update(
    *,
    client,
    version: str,
    sha256: str,
    staging_dir: Path = STAGING_DIR,
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
) -> Path:
    """Download, verify, stage an update and start the swap helper.

    Returns the staged payload directory. The caller must only stop the service
    after this returns without raising.
    """

    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    # Drop whatever a previous failed attempt left behind before downloading again.
    await asyncio.to_thread(cleanup_staging, staging_dir)
    archive = staging_dir / f"observatory-agent-{version}.zip"

    if on_event:
        await on_event("update_downloading", {"version": version})
    await client.download_package(version, archive, sha256)
    log.info("downloaded agent package %s (%s bytes)", version, archive.stat().st_size)

    if on_event:
        await on_event("update_unpacking", {"version": version})
    payload_root = extract_payload(archive, staging_dir / f"payload-{version}")
    (payload_root / "VERSION").write_text(version + "\n", encoding="utf-8")

    helper = spawn_apply_helper(payload_root, version)
    if helper is None:
        # Never let the caller stop the service for an update that has no one to
        # finish it: a stopped service with a dead helper is a silent brick.
        raise RuntimeError("无法启动更新助手，已保留当前版本继续运行")
    if not verify_helper_alive(helper):
        raise RuntimeError("更新助手启动即退出，已保留当前版本继续运行")
    log.info("update helper started for %s -> %s", version, payload_root)
    return payload_root


def cleanup_staging(staging_dir: Path = STAGING_DIR, *, keep_archive: bool = False) -> None:
    """Remove leftovers from previous update attempts.

    A failed attempt used to leave the downloaded zip, the unpacked payload and a
    stale helper behind; repeated retries then filled %ProgramData% with dozens of
    megabytes. Anything older than the current attempt is safe to drop because the
    service only stops once a helper is confirmed running.
    """

    staging_dir = Path(staging_dir)
    if not staging_dir.is_dir():
        return
    for path in staging_dir.iterdir():
        try:
            if path.is_dir() and path.name.startswith(("payload-", "apply-")):
                shutil.rmtree(path, ignore_errors=True)
            elif path.name == "observatory-updater.exe":
                # Legacy helper copied from an onedir build; it can never run.
                path.unlink(missing_ok=True)
            elif path.suffix == ".part":
                path.unlink(missing_ok=True)
            elif path.suffix == ".zip" and not keep_archive:
                path.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("could not clean %s: %s", path, exc)


def verify_helper_alive(helper: subprocess.Popen, grace_s: float = 2.5) -> bool:
    """Confirm the detached helper survived its own startup.

    The helper re-executes this same executable, so a bad argument list or a
    missing payload kills it within milliseconds — after the service has already
    agreed to stop. That is exactly how 0.2.0 devices went silent: the helper
    exited on an argparse error and nothing restarted the service. Checking here
    turns that class of failure into a refused update instead of a dead agent.
    """

    deadline = time.time() + max(0.0, grace_s)
    while time.time() < deadline:
        code = helper.poll()
        if code is not None:
            log.error("update helper exited immediately (code %s); aborting update", code)
            return False
        time.sleep(0.15)
    return True


def extract_payload(archive: Path, destination: Path) -> Path:
    """Unpack a zip and return the directory that actually holds the exe."""

    import zipfile

    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as archive_file:
        for member in archive_file.infolist():
            # Defence in depth: `extract` already strips absolute paths and "..",
            # but a package that tries to escape the staging directory should be
            # rejected loudly instead of quietly unpacking somewhere else.
            target = (root / member.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(f"安装包包含非法路径: {member.filename}") from exc
        archive_file.extractall(destination)

    candidates = [destination]
    candidates.extend([item for item in destination.iterdir() if item.is_dir()])
    for candidate in candidates:
        if any(candidate.glob("*.exe")):
            return candidate
    return destination


def spawn_apply_helper(payload_root: Path, version: str) -> subprocess.Popen | None:
    """Start a helper that can replace the install directory.

    Two hard constraints shape this:

    * The build is a PyInstaller ``onedir`` bundle, so an executable only runs
      next to its ``_internal`` directory; copying just the exe into staging made
      the helper die on startup and the update was refused forever.
    * The helper cannot run from the payload directory itself, because applying
      the update renames that directory into place with ``os.replace``.

    So the payload is copied to a private staging directory first, and the helper
    runs from there.
    """

    payload_root = Path(payload_root)
    workdir = STAGING_DIR / f"apply-{version}-{int(time.time() * 1000) % 10_000_000}"
    try:
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)
        shutil.copytree(payload_root, workdir)
    except OSError as exc:
        log.error("could not stage updater: %s", exc)
        return None

    helper = workdir / EXE_NAME
    if not helper.is_file():
        candidates = list(workdir.glob("*.exe"))
        if not candidates:
            log.error("no executable in payload %s", payload_root)
            return None
        helper = candidates[0]

    arguments = [
        str(helper),
        "--apply-update",
        str(payload_root),
        "--target",
        str(INSTALL_DIR),
        "--pid",
        str(os.getpid()),
        "--version-name",
        version,
    ]
    try:
        return subprocess.Popen(
            arguments,
            cwd=str(workdir),
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            # Keep the helper's stderr: when it fails to start, the reason is the
            # only evidence that survives the service stopping itself.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        log.error("could not start updater: %s", exc)
        return None


# --------------------------------------------------------------- helper mode ---
def apply_now(payload_root: str, target_dir: str, wait_pid: int, version: str) -> int:
    """Runs in the detached helper. Blocks until the swap is done.

    Return codes (also logged): 0 ok, 2 payload missing, 3 swap failed,
    4 payload failed its pre-flight check, 5 rolled back after the new build
    would not start, 6 rollback itself failed.
    """

    from .logging_setup import setup_logging

    setup_logging("updater")
    log.info("updater starting: %s -> %s (waiting for pid %s)", payload_root, target_dir, wait_pid)

    source = Path(payload_root)
    target = Path(target_dir)

    if not source.is_dir():
        log.error("payload directory missing: %s", source)
        return 2

    # Last-ditch sanity check on the swap target. A layout mistake in
    # INSTALL_DIR (it once resolved to ``<install>\_internal``) would otherwise
    # rename the *support* directory, leaving the exe at the root pointing at a
    # Python runtime that is no longer there. The install root is the one that
    # holds the executable, so refuse anything else.
    if not (target / EXE_NAME).is_file():
        log.error(
            "refusing to swap %s: it does not contain %s, so it is not the install root",
            target,
            EXE_NAME,
        )
        return 7

    # Pre-flight: prove the staged build starts before touching the live install.
    # Doing this first means a broken payload leaves the running service alone
    # instead of stopping it and leaving the machine with nothing to restart.
    ok, detail = verify_payload(source, version)
    if not ok:
        log.error("refusing to install %s: %s (keeping the current build)", version, detail)
        return 4
    log.info("payload pre-flight ok (reports %s)", detail)

    _wait_for_process_exit(wait_pid, SWAP_WAIT_S)
    _stop_service()
    time.sleep(1.0)

    backup = target.with_name(target.name + f".old-{int(time.time())}")
    moved = False
    try:
        if target.exists():
            os.replace(target, backup)
            moved = True
        os.replace(source, target)
        log.info("swap complete")
    except OSError as exc:
        log.exception("swap failed: %s", exc)
        if moved and not target.exists():
            try:
                os.replace(backup, target)
                log.warning("rolled back to previous build")
            except OSError:
                log.error("rollback failed as well")
        _start_service()
        return 3

    # The swap is not the finish line: if the new build cannot run as a service,
    # put the previous one back rather than leaving the host offline.
    if not _start_service():
        log.error("new build did not come up; rolling back to the previous build")
        try:
            failed = target.with_name(target.name + f".failed-{int(time.time())}")
            os.replace(target, failed)
            if moved and backup.exists():
                os.replace(backup, target)
                moved = False
            if _start_service():
                log.warning("rolled back to previous build; update %s abandoned", version)
                return 5
            log.error("previous build did not start either; manual attention required")
        except OSError as exc:
            log.exception("rollback after failed start did not work: %s", exc)
        return 6

    _cleanup_old(backup)
    log.info("update to %s finished", version)
    return 0


def _wait_for_process_exit(pid: int, timeout_s: float) -> None:
    if pid <= 0:
        return
    try:
        import win32api
        import win32event
        import win32process

        handle = win32api.OpenProcess(win32process.SYNCHRONIZE, False, pid)
        try:
            win32event.WaitForSingleObject(handle, int(timeout_s * 1000))
        finally:
            win32api.CloseHandle(handle)
    except Exception as exc:  # noqa: BLE001
        log.debug("wait for pid failed: %s", exc)
        time.sleep(3)


def _stop_service() -> None:
    try:
        import win32service
        import win32serviceutil

        status = win32serviceutil.QueryServiceStatus("ObservatoryAgent")[1]
        if status == win32service.SERVICE_STOPPED:
            return
        win32serviceutil.StopService("ObservatoryAgent")
        deadline = time.time() + 60
        while time.time() < deadline:
            status = win32serviceutil.QueryServiceStatus("ObservatoryAgent")[1]
            if status == win32service.SERVICE_STOPPED:
                return
            time.sleep(0.5)
        log.warning("service did not stop in time")
    except Exception as exc:  # noqa: BLE001
        log.debug("stop service failed: %s", exc)


def _start_service() -> bool:
    """Bring the service back up, retrying; True once it reports RUNNING.

    A single ``StartService`` call used to be the last step of an update. If the
    SCM was still settling, or antivirus held the freshly written exe for longer
    than the start timeout, that one call failed, nothing retried, and the device
    simply stayed offline until somebody logged in. Retrying here is what makes
    the helper's last step reliable.
    """

    try:
        import win32service
        import win32serviceutil
    except Exception as exc:  # noqa: BLE001 - non-Windows or broken pywin32
        log.error("cannot manage the service: %s", exc)
        return False

    for attempt in range(1, SERVICE_START_ATTEMPTS + 1):
        try:
            status = win32serviceutil.QueryServiceStatus("ObservatoryAgent")[1]
        except Exception as exc:  # noqa: BLE001
            log.error("query service (attempt %s) failed: %s", attempt, exc)
            status = None

        if status == win32service.SERVICE_RUNNING:
            log.info("service is running")
            return True

        if status not in (
            win32service.SERVICE_START_PENDING,
            win32service.SERVICE_CONTINUE_PENDING,
        ):
            try:
                win32serviceutil.StartService("ObservatoryAgent")
                log.info("StartService issued (attempt %s)", attempt)
            except Exception as exc:  # noqa: BLE001
                log.error("StartService (attempt %s) failed: %s", attempt, exc)

        deadline = time.time() + SERVICE_START_SETTLE_S
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                status = win32serviceutil.QueryServiceStatus("ObservatoryAgent")[1]
            except Exception:  # noqa: BLE001
                continue
            if status == win32service.SERVICE_RUNNING:
                # RUNNING is not proof of success: a build that crashes right
                # after start-up also reports RUNNING for a moment. Confirm it is
                # still alive a few seconds later before declaring victory.
                time.sleep(SERVICE_CONFIRM_S)
                try:
                    status = win32serviceutil.QueryServiceStatus("ObservatoryAgent")[1]
                except Exception:  # noqa: BLE001
                    status = None
                if status == win32service.SERVICE_RUNNING:
                    log.info("service started and stayed up (attempt %s)", attempt)
                    return True
                log.warning("service came up then dropped to %s (attempt %s)", status, attempt)
                break
            if status == win32service.SERVICE_STOPPED:
                break

        log.warning("service not running after attempt %s, retrying", attempt)
        time.sleep(SERVICE_START_BACKOFF_S * attempt)

    log.error("could not start the service after %s attempts", SERVICE_START_ATTEMPTS)
    return False


def _cleanup_old(backup: Path) -> None:
    for attempt in range(5):
        try:
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=False)
            return
        except OSError:
            time.sleep(1.5 * (attempt + 1))


__all__ = [
    "apply_now",
    "apply_update",
    "cleanup_staging",
    "extract_payload",
    "spawn_apply_helper",
    "verify_helper_alive",
    "verify_payload",
]