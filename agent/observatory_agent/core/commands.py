"""命令分发与**受控端侧幂等**（规格 C3 / §5.3）。

0.2.10 的命令处理散在 ``runner._run_service_command``、``runner._delegate_to_worker``
和 ``worker._run_command`` 三处，每处各自拼回执字段；下行命令重复投递时也没有
任何去重，重启/重连窗口里可能执行两次（``files`` 删除、``update`` 交换目录都是
破坏性动作）。0.3.0 收成这一个入口：

- ``CommandDispatcher.dispatch(command)`` 是**唯一**的执行入口，返回
  ``CommandResult``，由链路层包装成 ``result`` 信封；
- 同一个 ``command.id`` 只真正执行一次，重复投递直接返回**同一份回执**
  （规格 §5.3：重复执行返回同一回执）；
- 未注册类型/处理器抛异常都产出**可观测的失败回执**，绝不静默（C6）。

纯逻辑：不读时钟、不做 IO、不 import link/win/tasks；时间由调用方注入。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

log = logging.getLogger(__name__)

#: 回执里 error_code 的取值（与规格 §5.5 的错误码对齐）。
ERROR_UNSUPPORTED: Final[str] = "UNSUPPORTED"
ERROR_BAD_REQUEST: Final[str] = "BAD_REQUEST"
ERROR_INTERNAL: Final[str] = "INTERNAL"

DEFAULT_RECEIPT_CACHE: Final[int] = 1024


@dataclass(frozen=True, slots=True)
class Command:
    """一条下行命令（``command`` 信封的 body）。"""

    id: str
    type: str
    payload: dict[str, Any]
    deadline_ms: int | None = None
    ts_ms: int = 0

    @classmethod
    def from_body(cls, body: Mapping[str, Any], *, ts_ms: int = 0) -> "Command":
        """从 ``command`` 信封 body 构造；结构错误抛 ``ValueError``。"""

        if not isinstance(body, Mapping):
            raise ValueError("命令 body 必须是对象")
        command_id = body.get("command_id") or body.get("id")
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("命令缺少 command_id")
        command_type = body.get("type")
        if not isinstance(command_type, str) or not command_type.strip():
            raise ValueError("命令缺少 type")
        payload = body.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise ValueError("命令 payload 必须是对象")
        deadline_raw = body.get("deadline_ms")
        deadline: int | None = None
        if isinstance(deadline_raw, int) and not isinstance(deadline_raw, bool) and deadline_raw > 0:
            deadline = deadline_raw
        return cls(
            id=command_id.strip(),
            type=command_type.strip(),
            payload=dict(payload),
            deadline_ms=deadline,
            ts_ms=int(ts_ms),
        )


@dataclass(frozen=True, slots=True)
class CommandResult:
    """命令执行结果；``to_body`` 产出上行 ``result`` 信封的 body。"""

    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None

    def to_body(
        self,
        command_id: str,
        *,
        device_time_ms: int,
        elapsed_ms: int = 0,
    ) -> dict[str, Any]:
        return {
            "command_id": command_id,
            "ok": bool(self.ok),
            "result": self.result,
            "error": self.error,
            "error_code": self.error_code,
            "elapsed_ms": int(elapsed_ms),
            "device_time_ms": int(device_time_ms),
        }


CommandHandler = Callable[[Command], "CommandResult | Awaitable[CommandResult]"]


class CommandDispatcher:
    """命令类型 → 处理器，带 command.id 回执缓存。

    ``handlers`` 是公开的属性：agent-sys 的 ``tasks/*`` 直接
    ``dispatcher.handlers["files"] = handler`` 即可，不强制走 ``register``。
    处理器可以是同步函数，也可以是协程函数。
    """

    def __init__(self, *, max_receipts: int = DEFAULT_RECEIPT_CACHE) -> None:
        if max_receipts <= 0:
            raise ValueError("max_receipts 必须为正")
        self.handlers: dict[str, CommandHandler] = {}
        self._max_receipts = max_receipts
        self._receipts: OrderedDict[str, CommandResult] = OrderedDict()
        self._executed = 0
        self._duplicates = 0
        self._failures = 0

    # -- 注册 ------------------------------------------------------------
    def register(self, command_type: str, handler: CommandHandler) -> None:
        if not command_type or not isinstance(command_type, str):
            raise ValueError("命令类型必须是非空字符串")
        if not callable(handler):
            raise TypeError(f"处理器不可调用：{handler!r}")
        self.handlers[command_type] = handler
        log.info("命令处理器已注册：%s", command_type)

    def register_many(self, handlers: Mapping[str, CommandHandler]) -> None:
        for command_type, handler in handlers.items():
            self.register(command_type, handler)

    def supported(self) -> tuple[str, ...]:
        return tuple(sorted(self.handlers))

    # -- 执行 ------------------------------------------------------------
    async def dispatch(self, command: Command) -> CommandResult:
        """执行一条命令；同 id 重复投递返回第一次的回执，不重复执行。"""

        cached = self._receipts.get(command.id)
        if cached is not None:
            self._duplicates += 1
            log.info("命令 %s 重复投递，返回既有回执（不重复执行）", command.id)
            return cached

        handler = self.handlers.get(command.type)
        if handler is None:
            result = CommandResult(
                ok=False,
                error=f"不支持的命令类型: {command.type}",
                error_code=ERROR_UNSUPPORTED,
            )
            log.warning("命令 %s 类型 %s 无处理器", command.id, command.type)
        else:
            self._executed += 1
            result = await self._invoke(handler, command)

        if not result.ok:
            self._failures += 1
        self._remember(command.id, result)
        return result

    async def _invoke(self, handler: CommandHandler, command: Command) -> CommandResult:
        try:
            outcome = handler(command)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 处理器失败必须变成可观测回执
            log.exception("命令 %s 类型 %s 执行抛异常", command.id, command.type)
            return CommandResult(ok=False, error=f"{type(exc).__name__}: {exc}", error_code=ERROR_INTERNAL)
        if not isinstance(outcome, CommandResult):
            log.error("命令 %s 处理器返回了非 CommandResult: %r", command.id, type(outcome).__name__)
            return CommandResult(
                ok=False,
                error=f"处理器返回类型错误: {type(outcome).__name__}",
                error_code=ERROR_INTERNAL,
            )
        return outcome

    # -- 回执缓存 --------------------------------------------------------
    def _remember(self, command_id: str, result: CommandResult) -> None:
        self._receipts[command_id] = result
        self._receipts.move_to_end(command_id)
        while len(self._receipts) > self._max_receipts:
            evicted, _value = self._receipts.popitem(last=False)
            log.debug("回执缓存淘汰 %s（上限 %s）", evicted, self._max_receipts)

    def receipt(self, command_id: str) -> CommandResult | None:
        return self._receipts.get(command_id)

    def stats(self) -> dict[str, int]:
        return {
            "executed": self._executed,
            "duplicates": self._duplicates,
            "failures": self._failures,
            "receipts": len(self._receipts),
            "handlers": len(self.handlers),
        }

    def clear(self) -> None:
        self._receipts.clear()


__all__ = [
    "DEFAULT_RECEIPT_CACHE",
    "ERROR_BAD_REQUEST",
    "ERROR_INTERNAL",
    "ERROR_UNSUPPORTED",
    "Command",
    "CommandDispatcher",
    "CommandHandler",
    "CommandResult",
]