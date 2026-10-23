"""Command journal — the single command creation and result point (C3).

Every console/file command is persisted as a ``commands`` row first and only
then delivered to the agent. Waiting is always scoped: ``async with
journal.expect(command_id)`` removes the waiter on exit, so a command that is
never awaited cannot leak an entry (A9).

Persistence is SQLite through ``core.models.Command``; retry/redelivery beyond
the session outbox belongs to W1 (``pending_for_device`` is the hook for it).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.clock import Clock, SYSTEM_CLOCK, now_ms
from ..core.config import Settings, get_settings
from ..core.errors import AppError
from ..core.models import Command
from ..core.log import log_event
from .envelope import Result, TERMINAL_RESULT_STATES

log = logging.getLogger(__name__)

#: Commands whose result has been reported are finished from the bus' point of view.
TERMINAL_COMMAND_STATUSES: frozenset[str] = frozenset({"ok", "error", "timeout", "cancelled"})


@dataclass(frozen=True, slots=True)
class CommandRecord:
    id: str
    device_id: int
    kind: str
    body: dict[str, Any]
    status: str
    result: dict[str, Any] | None
    error: str | None
    error_code: str | None
    attempts: int
    actor: str
    created_at_ms: int
    dispatched_at_ms: int | None
    finished_at_ms: int | None
    expires_at_ms: int
    deadline_ms: int

    @classmethod
    def from_model(cls, row: Command) -> CommandRecord:
        return cls(
            id=row.id,
            device_id=int(row.device_id),
            kind=row.type,
            body=dict(row.payload or {}),
            status=row.status,
            result=dict(row.result) if isinstance(row.result, dict) else None,
            error=row.error,
            error_code=row.error_code,
            attempts=int(row.attempts or 0),
            actor=row.actor,
            created_at_ms=int(row.created_at_ms),
            dispatched_at_ms=row.dispatched_at_ms,
            finished_at_ms=row.finished_at_ms,
            expires_at_ms=int(row.expires_at_ms),
            deadline_ms=int(row.deadline_ms or 0),
        )

    def to_envelope_body(self, *, deadline_ms: int | None = None) -> dict[str, Any]:
        """The downlink ``command`` body (spec §5.6, field-for-field)."""

        return {
            "command_id": self.id,
            "type": self.kind,
            "payload": dict(self.body),
            "deadline_ms": int(deadline_ms if deadline_ms is not None else self.deadline_ms),
        }

    def to_legacy(self) -> dict[str, Any]:
        """0.2.x ``Command`` shape used by the legacy compatibility layer."""

        return {
            "id": self.id,
            "type": self.kind,
            "status": self.status,
            "payload": dict(self.body),
            "result": self.result,
            "error": self.error,
            "created_at_ms": self.created_at_ms,
            "dispatched_at_ms": self.dispatched_at_ms,
            "finished_at_ms": self.finished_at_ms,
        }


class WaiterSlot:
    """One scoped waiter. ``settle`` is safe to call more than once."""

    __slots__ = ("command_id", "_event", "_result")

    def __init__(self, command_id: str) -> None:
        self.command_id = command_id
        self._event = asyncio.Event()
        self._result: Result | None = None

    @property
    def settled(self) -> bool:
        return self._result is not None

    def settle(self, result: Result) -> bool:
        if self._result is not None:
            return False
        self._result = result
        self._event.set()
        return True

    async def wait(self, timeout_s: float) -> Result | None:
        """Return the settled :class:`Result`, or ``None`` on timeout."""

        try:
            await asyncio.wait_for(self._event.wait(), timeout=max(0.01, timeout_s))
        except asyncio.TimeoutError:
            return None
        return self._result


class CommandJournal:
    """Persist commands and correlate the agent result with the waiting caller."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        settings: Settings | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings or get_settings()
        self._clock = clock or SYSTEM_CLOCK
        self._waiters: dict[str, WaiterSlot] = {}

    # -- creation / reads --------------------------------------------------
    async def create(
        self,
        device_id: int,
        kind: str,
        body: dict[str, Any] | None,
        *,
        actor: str = "console",
        timeout_ms: int | None = None,
        expires_at_ms: int | None = None,
    ) -> CommandRecord:
        now = self._clock.now_ms()
        budget = int(timeout_ms if timeout_ms is not None else self.deadline_for(kind))
        expiry = int(expires_at_ms if expires_at_ms is not None else now + max(budget * 3, 60_000))
        row = Command(
            id=uuid.uuid4().hex,
            device_id=int(device_id),
            type=str(kind),
            payload=dict(body or {}),
            status="pending",
            attempts=0,
            actor=str(actor),
            created_at_ms=now,
            expires_at_ms=expiry,
            deadline_ms=budget,
        )
        async with self._sessionmaker() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            record = CommandRecord.from_model(row)
        log_event(log, logging.DEBUG, "journal.created", command_id=record.id, device_id=device_id, kind=kind)
        return record

    async def list(
        self,
        device_id: int | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CommandRecord]:
        statement = select(Command).order_by(Command.created_at_ms.desc())
        if device_id is not None:
            statement = statement.where(Command.device_id == int(device_id))
        statement = statement.offset(max(0, offset)).limit(max(1, limit))
        async with self._sessionmaker() as session:
            rows = (await session.execute(statement)).scalars().all()
        return [CommandRecord.from_model(row) for row in rows]

    async def get(self, command_id: str) -> CommandRecord | None:
        async with self._sessionmaker() as session:
            row = await session.get(Command, command_id)
        return CommandRecord.from_model(row) if row is not None else None

    async def claim_pending(self, device_id: int, limit: int = 10) -> list[CommandRecord]:
        """Hand undelivered commands to the legacy poll carrier, marking them sent."""

        statement = (
            select(Command)
            .where(Command.device_id == int(device_id), Command.status == "pending")
            .order_by(Command.created_at_ms.asc())
            .limit(max(1, limit))
        )
        now = self._clock.now_ms()
        async with self._sessionmaker() as session:
            rows = list((await session.execute(statement)).scalars().all())
            if not rows:
                return []
            for row in rows:
                row.status = "dispatched"
                row.dispatched_at_ms = now
                row.attempts = int(row.attempts or 0) + 1
            await session.commit()
            records = [CommandRecord.from_model(row) for row in rows]
        log_event(log, logging.DEBUG, "journal.claimed", device_id=device_id, count=len(records))
        return records

    async def pending_for_device(self, device_id: int, limit: int = 50) -> list[CommandRecord]:
        """Hook for W1 outbox replay after reconnects (A10)."""

        statement = (
            select(Command)
            .where(
                Command.device_id == int(device_id),
                Command.status.in_(("pending", "dispatched")),
            )
            .order_by(Command.created_at_ms.asc())
            .limit(max(1, limit))
        )
        async with self._sessionmaker() as session:
            rows = (await session.execute(statement)).scalars().all()
        return [CommandRecord.from_model(row) for row in rows]

    # -- scoped waiting ----------------------------------------------------
    @asynccontextmanager
    async def expect(self, command_id: str) -> AsyncIterator[WaiterSlot]:
        """Register a waiter that is removed no matter how the block exits."""

        if command_id in self._waiters:
            raise AppError.internal(f"命令 {command_id} 已有等待方")
        slot = WaiterSlot(command_id)
        self._waiters[command_id] = slot
        try:
            yield slot
        finally:
            self._waiters.pop(command_id, None)

    def open_count(self) -> int:
        """Number of live scoped waiters (A9 asserts this returns to 0)."""

        return len(self._waiters)

    # -- settlement --------------------------------------------------------
    async def settle_result(
        self,
        command_id: str,
        *,
        ok: bool,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> CommandRecord | None:
        status = "ok" if ok else "error"
        return await self.settle_status(
            command_id, status, result=result, error=error, error_code=error_code
        )

    async def settle_status(
        self,
        command_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> CommandRecord | None:
        now = self._clock.now_ms()
        async with self._sessionmaker() as session:
            row = await session.get(Command, command_id)
            if row is None:
                log_event(log, logging.WARNING, "journal.settle_missing", command_id=command_id)
                return None
            record = CommandRecord.from_model(row)
            if record.status in TERMINAL_COMMAND_STATUSES:
                # First result wins: a late duplicate must not flip an outcome.
                log_event(
                    log,
                    logging.DEBUG,
                    "journal.settle_duplicate",
                    command_id=command_id,
                    status=record.status,
                )
                return record
            row.status = status
            row.result = dict(result) if isinstance(result, dict) else None
            row.error = error
            row.error_code = error_code
            row.finished_at_ms = now
            await session.commit()
            await session.refresh(row)
            record = CommandRecord.from_model(row)

        settle_ms = now
        result_value = Result(
            ok=status == "ok",
            status=status,
            result=record.result,
            error=record.error,
            error_code=record.error_code,
            retryable=status == "timeout",
            command_id=command_id,
            deadline_ms=record.deadline_ms,
            server_time_ms=settle_ms,
            latency_ms=max(0, settle_ms - record.created_at_ms),
        )
        slot = self._waiters.get(command_id)
        if slot is not None:
            slot.settle(result_value)
        return record

    async def mark_timeout(self, command_id: str, message: str = "受控端在规定时间内未响应") -> CommandRecord | None:
        return await self.settle_status(
            command_id,
            "timeout",
            error=message,
            error_code="TIMEOUT",
        )

    async def expire_stale(self, *, now: int | None = None) -> int:
        """Mark commands past their expiry as timeout; settles any waiter."""

        moment = now if now is not None else self._clock.now_ms()
        statement = select(Command).where(
            Command.status.in_(("pending", "dispatched")),
            Command.expires_at_ms > 0,
            Command.expires_at_ms < moment,
        )
        expired: list[str] = []
        async with self._sessionmaker() as session:
            rows = list((await session.execute(statement)).scalars().all())
            for row in rows:
                row.status = "timeout"
                row.error = "命令已过期（受控端未在期限内响应）"
                row.error_code = "TIMEOUT"
                row.finished_at_ms = moment
                expired.append(row.id)
            if expired:
                await session.commit()
        for command_id in expired:
            slot = self._waiters.get(command_id)
            if slot is not None:
                record = await self.get(command_id)
                slot.settle(
                    Result.timed_out(
                        command_id=command_id,
                        error="命令已过期（受控端未在期限内响应）",
                        deadline_ms=record.deadline_ms if record else 0,
                        server_time_ms=now_ms(),
                        latency_ms=max(0, moment - (record.created_at_ms if record else moment)),
                    )
                )
        if expired:
            log_event(log, logging.INFO, "journal.expired", count=len(expired))
        return len(expired)

    def deadline_for(self, kind: str) -> int:
        """The one budget source for this journal (§5.4)."""

        from ..core.clock import server_deadline_ms

        return server_deadline_ms(kind, self._settings)


__all__ = [
    "CommandJournal",
    "CommandRecord",
    "TERMINAL_COMMAND_STATUSES",
    "WaiterSlot",
]