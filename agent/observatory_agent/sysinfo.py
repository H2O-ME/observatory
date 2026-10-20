"""Hardware/OS snapshot and user-session enumeration.

Everything here degrades to a partial dictionary instead of raising: a machine with
an unusual disk layout or a locked-down WMI must still be usable, and a monitoring
agent that dies because ``psutil`` cannot read one counter is worse than useless.
"""

from __future__ import annotations

import getpass
import logging
import os
import platform
import socket
import time
from typing import Any

log = logging.getLogger(__name__)

WTS_ACTIVE = 0
WTS_CONNECTED = 1
WTS_DISCONNECTED = 4
WTS_IDLE = 5

STATE_NAMES = {
    0: "active",
    1: "connected",
    2: "connectquery",
    3: "shadow",
    4: "disconnected",
    5: "idle",
    6: "listen",
    7: "reset",
    8: "down",
    9: "init",
}


def collect(agent_version: str = "") -> dict[str, Any]:
    info: dict[str, Any] = {
        "collected_at_ms": int(time.time() * 1000),
        "agent_version": agent_version,
        "hostname": _safe(socket.gethostname),
        "fqdn": _safe(socket.getfqdn),
        "platform": platform.system(),
        "os_name": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "arch": platform.machine(),
        "username": _safe(getpass.getuser),
    }
    info.update(_cpu_memory())
    info.update(_disks())
    info["sessions"] = list_sessions()
    info["ips"] = _ips()
    info["boot_time_ms"] = _boot_time_ms()
    info["uptime_s"] = int(time.time() - (info["boot_time_ms"] / 1000)) if info["boot_time_ms"] else 0
    return info


def quick() -> dict[str, Any]:
    """Cheap subset sent with every heartbeat."""

    info: dict[str, Any] = {"collected_at_ms": int(time.time() * 1000)}
    info.update(_cpu_memory())
    info["uptime_s"] = _uptime_s()
    return info


def _safe(func, default: str = "") -> str:
    try:
        return str(func())
    except Exception:  # noqa: BLE001
        return default


def _cpu_memory() -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        import psutil

        result["cpu_percent"] = float(psutil.cpu_percent(interval=None))
        result["cpu_count"] = int(psutil.cpu_count(logical=True) or 0)
        result["cpu_physical"] = int(psutil.cpu_count(logical=False) or 0)
        try:
            freq = psutil.cpu_freq()
            result["cpu_mhz"] = int(freq.current) if freq else 0
        except Exception:  # noqa: BLE001
            result["cpu_mhz"] = 0
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            ) as key:
                result["cpu_model"] = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except Exception:  # noqa: BLE001
            result["cpu_model"] = platform.processor()

        memory = psutil.virtual_memory()
        result["mem_total"] = int(memory.total)
        result["mem_available"] = int(memory.available)
        result["mem_percent"] = float(memory.percent)
        swap = psutil.swap_memory()
        result["swap_total"] = int(swap.total)
        result["swap_percent"] = float(swap.percent)
    except Exception as exc:  # noqa: BLE001
        log.debug("psutil probe failed: %s", exc)
    return result


def _disks() -> dict[str, Any]:
    disks: list[dict[str, Any]] = []
    try:
        import psutil

        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            disks.append(
                {
                    "device": part.device,
                    "mount": part.mountpoint,
                    "fstype": part.fstype,
                    "total": int(usage.total),
                    "used": int(usage.used),
                    "free": int(usage.free),
                    "percent": float(usage.percent),
                }
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("disk probe failed: %s", exc)
    return {"disks": disks}


def _ips() -> list[str]:
    addresses: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in addresses and not address.startswith("127."):
                addresses.append(address)
    except Exception:  # noqa: BLE001
        pass
    return addresses[:8]


def _boot_time_ms() -> int:
    try:
        import psutil

        return int(psutil.boot_time() * 1000)
    except Exception:  # noqa: BLE001
        return 0


def _uptime_s() -> int:
    try:
        import psutil

        return int(time.time() - psutil.boot_time())
    except Exception:  # noqa: BLE001
        return 0


def list_sessions() -> list[dict[str, Any]]:
    """Enumerate interactive Windows sessions.

    Used by the service to decide which sessions need a capture worker and by the
    console to show who is logged in.
    """

    sessions: list[dict[str, Any]] = []
    try:
        import win32ts

        try:
            raw = win32ts.WTSEnumerateSessions(None)
        except Exception:  # noqa: BLE001 - older pywin32 takes (server, reserved, version)
            raw = win32ts.WTSEnumerateSessions(None, 0, 1)
        for entry in raw:
            session_id = int(entry.get("SessionId", 0))
            if session_id == 0:
                # Session 0 hosts services only; it has no desktop to capture.
                continue
            state = int(entry.get("State", 0))
            username = ""
            try:
                username = str(win32ts.WTSQuerySessionInformation(None, session_id, win32ts.WTSUserName) or "")
            except Exception:  # noqa: BLE001
                username = ""
            sessions.append(
                {
                    "session_id": session_id,
                    "username": username,
                    "state": STATE_NAMES.get(state, str(state)),
                    "is_console": session_id == _console_session_id(),
                    "station": str(entry.get("WinStationName") or entry.get("StationName") or ""),
                }
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("session enumeration failed: %s", exc)
    return sessions


def active_session_ids() -> list[int]:
    return [
        int(item["session_id"])
        for item in list_sessions()
        if item["state"] in {"active", "connected"}
    ]


def _console_session_id() -> int:
    try:
        import win32ts

        return int(win32ts.WTSGetActiveConsoleSessionId())
    except Exception:  # noqa: BLE001
        return 0


def console_session_id() -> int:
    return _console_session_id()


def current_session_id() -> int:
    try:
        import win32ts

        return int(win32ts.WTSGetActiveConsoleSessionId() if not _in_user_session() else _process_session_id())
    except Exception:  # noqa: BLE001
        return 0


def _process_session_id() -> int:
    try:
        import win32ts

        return int(win32ts.ProcessIdToSessionId(os.getpid()))
    except Exception:  # noqa: BLE001
        return 0


def _in_user_session() -> bool:
    return _process_session_id() != 0


__all__ = [
    "WTS_ACTIVE",
    "WTS_DISCONNECTED",
    "active_session_ids",
    "collect",
    "console_session_id",
    "current_session_id",
    "list_sessions",
    "quick",
]