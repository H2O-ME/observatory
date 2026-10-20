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


async def apply_update(
    *,
    client,
    version: str,
    sha256: str,
    staging_dir: Path = STAGING_DIR,
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
) -> Path:
    """Download, verify and prepare an update payload; returns its directory."""

    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
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
    log.info("update helper started for %s -> %s", version, payload_root)
    _ = helper
    return payload_root


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
    """Run a detached copy of ourselves to perform the swap."""

    helper = STAGING_DIR / "observatory-updater.exe"
    source = Path(sys.executable)
    try:
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, helper)
    except OSError as exc:
        log.error("could not stage updater: %s", exc)
        return None

    arguments = [
        str(helper),
        "--apply-update",
        str(payload_root),
        "--target",
        str(INSTALL_DIR),
        "--pid",
        str(os.getpid()),
        "--version",
        version,
    ]
    try:
        return subprocess.Popen(
            arguments,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        log.error("could not start updater: %s", exc)
        return None


# --------------------------------------------------------------- helper mode ---
def apply_now(payload_root: str, target_dir: str, wait_pid: int, version: str) -> int:
    """Runs in the detached helper. Blocks until the swap is done."""

    from .logging_setup import setup_logging

    setup_logging("updater")
    log.info("updater starting: %s -> %s (waiting for pid %s)", payload_root, target_dir, wait_pid)

    _wait_for_process_exit(wait_pid, SWAP_WAIT_S)
    _stop_service()
    time.sleep(1.0)

    source = Path(payload_root)
    target = Path(target_dir)
    backup = target.with_name(target.name + f".old-{int(time.time())}")

    if not source.is_dir():
        log.error("payload directory missing: %s", source)
        return 2

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

    _start_service()
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


def _start_service() -> None:
    try:
        import win32serviceutil

        win32serviceutil.StartService("ObservatoryAgent")
        log.info("service started")
    except Exception as exc:  # noqa: BLE001
        log.error("could not start service: %s", exc)


def _cleanup_old(backup: Path) -> None:
    for attempt in range(5):
        try:
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=False)
            return
        except OSError:
            time.sleep(1.5 * (attempt + 1))


__all__ = ["apply_now", "apply_update", "extract_payload", "spawn_apply_helper"]