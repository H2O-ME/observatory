"""Command bus: one await point shared by the WS path and the HTTP poll path.

The old implementation made the HTTP request block for up to 20s while it waited
for the agent (``fs.list`` did exactly that), which is why the file manager felt
broken and why a slow device could exhaust the request pool.

Here every command gets a ``Waiter`` the moment it is created. Whichever path
delivers the agent's answer — the WebSocket handler or the poll endpoint — calls
``settle`` once, and the original HTTP request wakes up immediately. Waiting is
bounded, cancellable and has no side effects on the device.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"ok", "error", "timeout", "cancelled"})


class CommandTimeoutError(TimeoutError):
    """Raised when the device did not answer within the deadline."""


@dataclass(slots=True)
class Waiter:
    command_id: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "pending"
    result: dict[str, Any] | None = None
    error: str | None = None

    @property
    def settled(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class CommandBus:
    def __init__(self) -> None:
        self._waiters: dict[str, Waiter] = {}

    # -- lifecycle ---------------------------------------------------------
    def open(self, command_id: str) -> Waiter:
        waiter = self._waiters.get(command_id)
        if waiter is None:
            waiter = Waiter(command_id=command_id)
            self._waiters[command_id] = waiter
        return waiter

    def peek(self, command_id: str) -> Waiter | None:
        return self._waiters.get(command_id)

    def forget(self, command_id: str) -> None:
        self._waiters.pop(command_id, None)

    def open_count(self) -> int:
        return sum(1 for waiter in self._waiters.values() if not waiter.settled)

    # -- settling ----------------------------------------------------------
    def settle(
        self,
        command_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """Resolve the waiter. Returns False when nobody was waiting."""

        waiter = self._waiters.get(command_id)
        if waiter is None:
            return False
        if waiter.settled:
            # First answer wins; a duplicate result must not flip a success into
            # a timeout afterwards.
            return False
        waiter.status = status
        waiter.result = result
        waiter.error = error
        waiter.event.set()
        return True

    # -- waiting -----------------------------------------------------------
    async def wait(self, command_id: str, timeout_s: float) -> Waiter:
        waiter = self.open(command_id)
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            if not waiter.settled:
                waiter.status = "timeout"
                waiter.error = f"受控端在 {timeout_s:.0f} 秒内没有响应"
                waiter.event.set()
            raise CommandTimeoutError(waiter.error) from exc
        return waiter

    def clear(self) -> None:
        for waiter in self._waiters.values():
            if not waiter.settled:
                waiter.status = "cancelled"
                waiter.error = "server shutting down"
                waiter.event.set()
        self._waiters.clear()


__all__ = ["CommandBus", "CommandTimeoutError", "TERMINAL_STATUSES", "Waiter"]