"""受控端日志：服务、worker、更新/卸载助手各写自己的文件。

0.2.10 的 ``logging_setup.py`` 只有一处实现，本来就不算重复；但 0.3.0 把它
挪进 ``core/``，与 ``core/config.py`` 的路径定义放在一起，并补上两点纪律：

- **敏感值脱敏**：设备令牌、注册码、服务端下发的秘密配置一律不得进日志
  （A4/A22）。``redact`` 是唯一入口，新代码需要打日志的敏感字段先过它。
- **异常必须可观测**：日志文件创建失败时不允许静默 ``pass``，至少向 stderr
  说一句——服务在无人值守机器上跑，日志是唯一的现场证据。

轮转按大小做（2 MiB × 3），避免长期运行的机器被日志占满磁盘。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

from .config import LOG_DIR, ensure_dirs

FORMAT = "%(asctime)s %(levelname)-7s [%(process)d:%(threadName)s] %(name)s: %(message)s"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
#: 永远不该出现在日志里的字段名（按键名匹配）。
SECRET_FIELD_HINTS = ("token", "secret", "password", "code", "authorization")


def redact(value: Any) -> str:
    """把敏感值变成可安全打印的短摘要；非敏感值原样 ``str``。"""

    text = str(value)
    if len(text) <= 4:
        return "***"
    return f"{text[:2]}***{text[-2:]}"


def setup_logging(
    role: str = "agent",
    *,
    level: int = logging.INFO,
    console: bool = False,
    directory: Path | None = None,
) -> Path:
    """配置根 logger，返回日志文件路径。

    ``role`` 决定文件名（``agent.log`` / ``worker-3.log`` / ``update.log``）；
    同一个进程只调用一次。测试可以用 ``directory`` 指到工作区内临时目录，
    避免写真实 ``%ProgramData%``。
    """

    target_dir = Path(directory) if directory is not None else Path(LOG_DIR)
    if directory is not None:
        target_dir.mkdir(parents=True, exist_ok=True)
    else:
        ensure_dirs()
    path = target_dir / f"{role}.log"

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(FORMAT)
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        # 没有日志文件也要留下证据：stderr 是 Windows 服务/前台控制台唯一的出口。
        print(f"警告：无法创建日志文件 {path}: {exc}", file=sys.stderr)

    if console or not getattr(sys, "frozen", False):
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # 第三方库默认太吵，且可能把 URL/头信息带进日志。
    for noise in ("httpx", "httpcore", "websockets", "asyncio"):
        logging.getLogger(noise).setLevel(logging.WARNING)
    return path


def read_tail(lines: int = 200, role: str = "agent", directory: Path | None = None) -> list[str]:
    """读日志尾部（供控制台 ``log_tail`` 命令使用）；失败返回空列表并记日志。"""

    target_dir = Path(directory) if directory is not None else Path(LOG_DIR)
    path = target_dir / f"{role}.log"
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            block = min(size, 256 * 1024)
            handle.seek(size - block)
            data = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        logging.getLogger(__name__).warning("读取日志尾部失败 %s: %s", path, exc)
        return []
    return data.splitlines()[-max(1, int(lines)) :]


__all__ = [
    "BACKUP_COUNT",
    "FORMAT",
    "MAX_BYTES",
    "SECRET_FIELD_HINTS",
    "read_tail",
    "redact",
    "setup_logging",
]