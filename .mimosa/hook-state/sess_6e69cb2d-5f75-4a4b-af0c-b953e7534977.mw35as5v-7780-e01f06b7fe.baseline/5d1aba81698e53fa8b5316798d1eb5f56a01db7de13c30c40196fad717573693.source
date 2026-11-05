"""命令分发与**受控端侧幂等**（规格 C3 / §5.3）。

0.2.10 的命令处理散在 ``runner._run_service_command``、``runner._delegate_to_worker``
和 ``worker._run_command`` 三处，每处各自拼回执字段；下行命令重复投递时也没有
任何去重，重启/重连窗口里可能执行两次（``files`` 删除、``update`` 交换目录都是
破坏性动作）。0.3.0 收成这一个入口：

- ``CommandDispatcher.dispatch(command)`` 是**唯一**的执行入口，返回
  ``CommandResult``，由链路层包装成 ``result`` 信封；
- 同一个 ``command.id`` 只真正执行一次，重复投递直接返回**同一份回执**
  （规格 §5.3：重复执行返回同一回执）；执行中的并发重投返回 IN_FLIGHT 占位
  回执，不并发执行（F38：回执缓存与 in-flight 集合全程持锁）；
- 未注册类型/处理器抛异常都产出**可观测的失败回执**，绝不静默（C6）。

纯逻辑：不读时钟、不做 IO、不 import link/win/tasks；时间由调用方注入。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

log = logging.getLogger(__name__)

#: 回执里 error_code 的取值（与规格 §5.5 的错误码对齐）。
ERROR_UNSUPPORTED: Final[str] = "UNSUPPORTED"
ERROR_BAD_REQUEST: Final[str] = "BAD_REQUEST"
ERROR_INTERNAL: Final[str] = "INTERNAL"
#: A6：命令在**执行前**就已超出预算（服务端 ``deadline_ms``）。服务端把
#: ``error_code == "TIMEOUT"`` 映射成 504（见 ``console/files.py`` 的
#: ``_result_to_error``），所以超时必须是明确的 TIMEOUT，而不是"执行成功"。
ERROR_TIMEOUT: Final[str] = "TIMEOUT"
#: F38：同 id 命令仍在执行中（并发重投）时的占位回执错误码。它**不会**进
#: 回执缓存——服务端稍后重投时仍能拿到正式回执。
ERROR_IN_FLIGHT: Final[str] = "IN_FLIGHT"

DEFAULT_RECEIPT_CACHE: Final[int] = 1024


@dataclass(frozen=True, slots=True)
class Command:
    """一条下行命令（``command`` 信封的 body）。

    ``deadline_ms`` 是服务端给的**执行预算**（相对毫秒数），不是绝对时间戳；
    ``ts_ms`` 必须是**同一进程内与判断时钟同源**的接收时刻。0.3.x 之前
    ``deadline_ms`` 只被保存、从不检查：命令在 worker 线程池里排队几十秒后
    照样执行，而用户早已收到超时，于是"删除/写入/重启"这类破坏性动作会在
    后台悄悄发生（A6）。
    """

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

    # -- 预算（A6） ------------------------------------------------------
    @property
    def deadline_at_ms(self) -> int | None:
        """绝对截止时刻；没有预算或没有接收时刻时返回 ``None``（不限制）。"""

        if not self.deadline_ms or self.ts_ms <= 0:
            return None
        return int(self.ts_ms) + int(self.deadline_ms)

    def remaining_ms(self, now_ms: int) -> int | None:
        deadline_at = self.deadline_at_ms
        if deadline_at is None:
            return None
        return max(0, deadline_at - int(now_ms))

    def is_expired(self, now_ms: int) -> bool:
        """是否已超出执行预算（纯函数，时间由调用方注入）。"""

        deadline_at = self.deadline_at_ms
        return deadline_at is not None and int(now_ms) > deadline_at

    def with_receipt(self, *, ts_ms: int) -> "Command":
        """换一个接收时刻（同一命令跨进程转发时用：每个进程用自己的时钟起算）。"""

        return Command(
            id=self.id,
            type=self.type,
            payload=self.payload,
            deadline_ms=self.deadline_ms,
            ts_ms=int(ts_ms),
        )


def expired_result(command: "Command") -> CommandResult:
    """预算已耗尽的回执（A6）：明确的 TIMEOUT，绝不执行动作。"""

    return CommandResult(
        ok=False,
        error=(
            f"命令超出执行预算（deadline_ms={command.deadline_ms}），未执行；"
            "如仍需执行请重新下发命令"
        ),
        error_code=ERROR_TIMEOUT,
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
        # F38：回执缓存的读写可能来自 3 个 worker 线程（消费下行命令的线程池），
        # 同一 command_id 重投时无锁的 get/put 会让破坏性命令重复执行。
        self._lock = threading.Lock()
        self._receipts: OrderedDict[str, CommandResult] = OrderedDict()
        self._inflight: set[str] = set()
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
        """执行一条命令；同 id 重复投递返回第一次的回执，不重复执行。

        F38：``_receipts`` 的读写与 in-flight 标记全程持 ``_lock``——3 个
        worker 线程并发 dispatch 时，同 id 重投要么命中已缓存回执、要么命中
        in-flight 占位回执，绝不会并发执行两次破坏性命令。in-flight 占位回执
        **不写缓存**：执行完成后重投的请求拿到的是正式回执。
        """

        with self._lock:
            cached = self._receipts.get(command.id)
            if cached is None and command.id in self._inflight:
                busy = True
            else:
                busy = False
                if cached is None:
                    self._inflight.add(command.id)
        if cached is not None:
            self._duplicates += 1
            log.info("命令 %s 重复投递，返回既有回执（不重复执行）", command.id)
            return cached
        if busy:
            self._duplicates += 1
            log.warning("命令 %s 正在执行中（并发重投），返回占位回执", command.id)
            return CommandResult(
                ok=False,
                error="命令正在执行中，请等待执行完成后重投获取正式回执",
                error_code=ERROR_IN_FLIGHT,
            )

        try:
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

            with self._lock:
                if not result.ok:
                    self._failures += 1
                self._remember(command.id, result)
            return result
        finally:
            with self._lock:
                self._inflight.discard(command.id)

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
        # 调用方已持锁（dispatch）；公开的 receipt/clear/stats 各自加锁。
        self._receipts[command_id] = result
        self._receipts.move_to_end(command_id)
        while len(self._receipts) > self._max_receipts:
            evicted, _value = self._receipts.popitem(last=False)
            log.debug("回执缓存淘汰 %s（上限 %s）", evicted, self._max_receipts)

    def receipt(self, command_id: str) -> CommandResult | None:
        with self._lock:
            return self._receipts.get(command_id)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "executed": self._executed,
                "duplicates": self._duplicates,
                "failures": self._failures,
                "receipts": len(self._receipts),
                "handlers": len(self.handlers),
            }

    def clear(self) -> None:
        with self._lock:
            self._receipts.clear()
            self._inflight.clear()


__all__ = [
    "DEFAULT_RECEIPT_CACHE",
    "ERROR_BAD_REQUEST",
    "ERROR_IN_FLIGHT",
    "ERROR_INTERNAL",
    "ERROR_TIMEOUT",
    "ERROR_UNSUPPORTED",
    "Command",
    "CommandDispatcher",
    "CommandHandler",
    "CommandResult",
    "expired_result",
]