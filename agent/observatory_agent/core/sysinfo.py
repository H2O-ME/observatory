"""硬件/操作系统快照（hello / heartbeat 的 ``sysinfo`` 字段）。

0.2.10 的 ``sysinfo.py`` 位于包根目录，属于"要被清理的旧实现"，而且它把
WTS 会话枚举和硬件采样混在一个模块里。0.3.0 拆开：

- 本模块只采样**主机**信息，不 import ``win``（依赖方向保持 main -> win/tasks -> core）；
  会话列表由调用方（服务运行时）从 ``win.session`` 取到后作为参数传进来。
- 任何一项探测失败都降级为"缺字段 + 一条 warning"，不允许因为读不到某个计数器
  就让心跳整体失败（0.2.10 的注释说过同样的理由，这里保留这条纪律）。

时间戳字段：``collected_at_ms`` 是本机时钟；上行协议里叫 ``device_time_ms`` 的
时间由链路统一补（C5），本模块不参与服务端时钟校准。
"""

from __future__ import annotations

import getpass
import logging
import platform
import socket
import time
from collections.abc import Iterable, Mapping
from typing import Any

log = logging.getLogger(__name__)


def collect(agent_version: str = "", *, sessions: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """完整快照（注册/重注册/sysinfo 命令用；比 ``quick`` 贵）。"""

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
    info["sessions"] = [dict(item) for item in (sessions or [])]
    info["ips"] = _ips()
    boot = _boot_time_ms()
    info["boot_time_ms"] = boot
    info["uptime_s"] = int(time.time() - boot / 1000) if boot else 0
    return info


def quick() -> dict[str, Any]:
    """心跳用的廉价子集（每 20s 一次，必须快）。"""

    info: dict[str, Any] = {"collected_at_ms": int(time.time() * 1000)}
    info.update(_cpu_memory())
    info["uptime_s"] = _uptime_s()
    return info


def _safe(func: Any, default: str = "") -> str:
    try:
        return str(func())
    except Exception as exc:  # noqa: BLE001 - 单项探测失败不应让快照整体失败
        log.debug("sysinfo 探测 %r 失败: %s", getattr(func, "__name__", func), exc)
        return default


def _cpu_memory() -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        import psutil
    except ImportError as exc:  # pragma: no cover - psutil 是部署依赖
        log.warning("psutil 不可用，CPU/内存字段缺失: %s", exc)
        return result
    try:
        result["cpu_percent"] = float(psutil.cpu_percent(interval=None))
        result["cpu_count"] = int(psutil.cpu_count(logical=True) or 0)
        result["cpu_physical"] = int(psutil.cpu_count(logical=False) or 0)
        try:
            frequency = psutil.cpu_freq()
            result["cpu_mhz"] = int(frequency.current) if frequency else 0
        except Exception as exc:  # noqa: BLE001 - 虚拟机常不支持频率查询
            log.debug("读取 CPU 频率失败: %s", exc)
            result["cpu_mhz"] = 0
        result["cpu_model"] = _cpu_model()
        memory = psutil.virtual_memory()
        result["mem_total"] = int(memory.total)
        result["mem_available"] = int(memory.available)
        result["mem_percent"] = float(memory.percent)
        swap = psutil.swap_memory()
        result["swap_total"] = int(swap.total)
        result["swap_percent"] = float(swap.percent)
    except Exception as exc:  # noqa: BLE001 - 采样异常降级为缺字段
        log.warning("CPU/内存采样失败: %s", exc)
    return result


def _cpu_model() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        ) as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except (ImportError, OSError) as exc:
        log.debug("读取 CPU 型号失败（改用 platform.processor）: %s", exc)
        return platform.processor()


def _disks() -> dict[str, Any]:
    disks: list[dict[str, Any]] = []
    try:
        import psutil
    except ImportError as exc:  # pragma: no cover - 部署依赖
        log.warning("psutil 不可用，磁盘字段缺失: %s", exc)
        return {"disks": disks}
    try:
        for partition in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(partition.mountpoint)
            except OSError as exc:
                # 空光驱/未就绪的移动盘很常见：跳过是预期行为，不是错误。
                log.debug("跳过不可用分区 %s: %s", partition.mountpoint, exc)
                continue
            disks.append(
                {
                    "device": partition.device,
                    "mount": partition.mountpoint,
                    "fstype": partition.fstype,
                    "total": int(usage.total),
                    "used": int(usage.used),
                    "free": int(usage.free),
                    "percent": float(usage.percent),
                }
            )
    except Exception as exc:  # noqa: BLE001 - 枚举异常降级为已有结果
        log.warning("磁盘采样失败: %s", exc)
    return {"disks": disks}


def _ips() -> list[str]:
    addresses: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in addresses and not address.startswith("127."):
                addresses.append(address)
    except Exception as exc:  # noqa: BLE001 - DNS 失败只影响展示
        log.debug("解析本机地址失败: %s", exc)
    return addresses[:8]


def _boot_time_ms() -> int:
    try:
        import psutil

        return int(psutil.boot_time() * 1000)
    except Exception as exc:  # noqa: BLE001 - 采样失败返回 0，由调用方展示为未知
        log.debug("读取启动时间失败: %s", exc)
        return 0


def _uptime_s() -> int:
    boot = _boot_time_ms()
    if not boot:
        return 0
    return max(0, int(time.time() - boot / 1000))


__all__ = ["collect", "quick"]