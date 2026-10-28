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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.clock import Clock, SYSTEM_CLOCK, now_ms
from ..core.config import Settings, get_settings
from ..core.errors import AppError
from ..core.models import AuditEntry, Command, Device
from ..core.log import log_event
from .envelope import Result

log = logging.getLogger(__name__)

#: Commands whose result has been reported are finished from the bus' point of view.
#: Rows in a terminal status never change again (F20: `"failed"` is terminal —
#: without it, REPLAY_EXHAUSTED rows leaked forever and a late result could
#: overwrite an exhausted replay). ``unconfirmed`` is terminal for *evidence*
#: purposes: it means "we never learned the outcome". Only a real ACK, which is
#: new evidence, may upgrade it (see :meth:`CommandJournal.settle_ack`).
TERMINAL_COMMAND_STATUSES: frozenset[str] = frozenset(
    {"ok", "error", "timeout", "cancelled", "failed", "unconfirmed"}
)

#: Statuses with outstanding work: still to be delivered, or delivered and
#: executing. Used for replay (never re-deliver ``running``) and for UI state.
OPEN_COMMAND_STATUSES: tuple[str, ...] = ("pending", "dispatched", "running")

#: Why a command is in ``unconfirmed``: the agent never told us the outcome.
ACK_TIMEOUT = "ACK_TIMEOUT"

#: Why a command failed after the agent did report a failure.
UNINSTALL_FAILED = "UNINSTALL_FAILED"

#: Replay budget: initial delivery + up to 5 reconnect attempts, then give up.
MAX_REPLAY_ATTEMPTS = 5
#: expire_stale handles at most this many rows per UPDATE (F59: bound one tick).
EXPIRE_BATCH = 500
REPLAY_BACKOFF_BASE_MS = 1_000
REPLAY_BACKOFF_MAX_MS = 60_000
REPLAY_EXHAUSTED = "REPLAY_EXHAUSTED"


def replay_backoff_ms(attempts: int) -> int:
    """Delay before replay attempt ``attempts + 1``: 1s, 2s, 4s … capped at 60s."""

    if attempts <= 0:
        return 0
    return min(REPLAY_BACKOFF_BASE_MS * (2 ** (attempts - 1)), REPLAY_BACKOFF_MAX_MS)


@dataclass(frozen=True, slots=True)
class CommandRecord:
    id: str
    #: ``None`` once the device record was removed (P3 keeps the command row).
    device_id: int | None
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
    device_uid: str = ""

    @classmethod
    def from_model(cls, row: Command) -> CommandRecord:
        return cls(
            id=row.id,
            device_id=int(row.device_id) if row.device_id is not None else None,
            device_uid=str(getattr(row, "device_uid", "") or ""),
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


#: ``(record, late_status, late_result, late_error) -> None`` — F6 hook.
LateResultListener = Callable[["CommandRecord", str, dict[str, Any] | None, str | None], Awaitable[None]]


class CommandJournal:
    """Persist commands and correlate the agent result with the waiting caller."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        settings: Settings | None = None,
        clock: Clock | None = None,
        late_result_listener: LateResultListener | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings or get_settings()
        self._clock = clock or SYSTEM_CLOCK
        self._waiters: dict[str, WaiterSlot] = {}
        #: F6: notified when a real result arrives after the caller already saw
        #: ``timeout`` — the console's view and the row disagree, which must be
        #: observable instead of a DEBUG-only log line.
        self._late_result_listener = late_result_listener

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
        device_uid: str = "",
    ) -> CommandRecord:
        now = self._clock.now_ms()
        budget = int(timeout_ms if timeout_ms is not None else self.deadline_for(kind))
        expiry = int(expires_at_ms if expires_at_ms is not None else now + max(budget * 3, 60_000))
        row = Command(
            id=uuid.uuid4().hex,
            device_id=int(device_id),
            # P3: denormalised so the row stays attributable after the device row
            # is deleted (``device_id`` becomes NULL then).
            device_uid=str(device_uid or "")[:64],
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

    async def list_by_uid(
        self,
        uid: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CommandRecord]:
        """Command history keyed by ``device_uid`` (P3).

        After a confirmed uninstall removes the device record, ``device_id`` is
        NULL on the affected rows; ``device_uid`` is what still answers "what
        happened to that machine" without the device being in the list.
        """

        statement = select(Command).order_by(Command.created_at_ms.desc())
        if uid:
            statement = statement.where(Command.device_uid == str(uid))
        else:
            statement = statement.where(Command.device_id.is_(None))
        statement = statement.offset(max(0, offset)).limit(max(1, limit))
        async with self._sessionmaker() as session:
            rows = (await session.execute(statement)).scalars().all()
        return [CommandRecord.from_model(row) for row in rows]

    async def get(self, command_id: str) -> CommandRecord | None:
        async with self._sessionmaker() as session:
            row = await session.get(Command, command_id)
        return CommandRecord.from_model(row) if row is not None else None

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

    async def replay_for_device(self, device_id: int, limit: int = 50) -> list[CommandRecord]:
        """Replay undelivered commands after a carrier reconnect (at-least-once).

        Rows stay ``dispatched`` so a lost result triggers another replay; the
        stable command id lets the agent dedupe execution (at-most-once). Replays
        are capped at :data:`MAX_REPLAY_ATTEMPTS` with exponential backoff; past
        that the command becomes ``failed/REPLAY_EXHAUSTED`` and is audited.
        Expired commands are skipped (housekeeping marks them timeout).
        """

        now = self._clock.now_ms()
        statement = (
            select(Command)
            .where(
                Command.device_id == int(device_id),
                Command.status.in_(("pending", "dispatched")),
                or_(Command.expires_at_ms == 0, Command.expires_at_ms > now),
            )
            .order_by(Command.created_at_ms.asc())
            .limit(max(1, limit))
        )
        async with self._sessionmaker() as session:
            rows = list((await session.execute(statement)).scalars().all())
            if not rows:
                return []
            replayed: list[Command] = []
            exhausted: list[tuple[Command, int]] = []
            for row in rows:
                attempts = int(row.attempts or 0)
                # F20: every state advance is a conditional UPDATE keyed on the
                # non-terminal statuses — a result that settles concurrently
                # makes the rowcount 0 and this replay skips the row.
                if attempts >= MAX_REPLAY_ATTEMPTS:
                    outcome = await session.execute(
                        update(Command)
                        .where(Command.id == row.id, Command.status.in_(("pending", "dispatched")))
                        .values(
                            status="failed",
                            error_code=REPLAY_EXHAUSTED,
                            error=f"replay exhausted after {attempts} attempts",
                            finished_at_ms=now,
                        )
                    )
                    if outcome.rowcount == 1:
                        exhausted.append((row, attempts))
                    continue
                last = int(row.dispatched_at_ms or 0)
                if attempts > 0 and last and now - last < replay_backoff_ms(attempts):
                    continue
                outcome = await session.execute(
                    update(Command)
                    .where(Command.id == row.id, Command.status.in_(("pending", "dispatched")))
                    .values(status="dispatched", dispatched_at_ms=now, attempts=attempts + 1)
                )
                if outcome.rowcount == 1:
                    row.attempts = attempts + 1
                    replayed.append(row)
            if exhausted:
                device = await session.get(Device, int(device_id))
                for row, attempts in exhausted:
                    session.add(
                        AuditEntry(
                            ts_ms=now,
                            actor="server",
                            action="command.replay_exhausted",
                            device_uid=(device.uid if device is not None else ""),
                            device_name=(device.display_name if device is not None else ""),
                            detail={
                                "command_id": row.id,
                                "type": row.type,
                                "attempts": attempts,
                                "error_code": REPLAY_EXHAUSTED,
                            },
                        )
                    )
            await session.commit()
            records = [CommandRecord.from_model(row) for row in replayed]
            exhausted_records = [(row.id, row.type, attempts) for row, attempts in exhausted]
        log_event(
            log,
            logging.INFO,
            "journal.replayed",
            device_id=device_id,
            count=len(records),
        )
        if exhausted_records:
            log_event(
                log,
                logging.WARNING,
                "journal.replay_exhausted",
                device_id=device_id,
                count=len(exhausted_records),
                commands=exhausted_records,
            )
        return records

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
        """Settle with a **conditional** UPDATE (F20/C10).

        ``WHERE status NOT IN (terminal)`` + rowcount decides the winner, so a
        concurrent ``mark_timeout``/``expire_stale`` and a late agent result can
        never interleave into a lost update: first writer wins, the loser reads
        the winner's row back.
        """

        now = self._clock.now_ms()
        async with self._sessionmaker() as session:
            outcome = await session.execute(
                update(Command)
                .where(Command.id == str(command_id), Command.status.not_in(TERMINAL_COMMAND_STATUSES))
                .values(
                    status=str(status),
                    result=dict(result) if isinstance(result, dict) else None,
                    error=error,
                    error_code=error_code,
                    finished_at_ms=now,
                )
            )
            await session.commit()
            row = (await session.execute(select(Command).where(Command.id == str(command_id)))).scalars().first()
            if row is None:
                log_event(log, logging.WARNING, "journal.settle_missing", command_id=command_id)
                return None
            record = CommandRecord.from_model(row)
            if outcome.rowcount != 1:
                # First result wins: a late duplicate must not flip an outcome.
                log_event(
                    log,
                    logging.DEBUG,
                    "journal.settle_duplicate",
                    command_id=command_id,
                    status=record.status,
                )
                if record.status == "timeout" and status in {"ok", "error"} and self._late_result_listener:
                    try:
                        await self._late_result_listener(record, status, result, error)
                    except Exception as exc:  # noqa: BLE001 - observability must not fail settlement
                        log_event(log, logging.WARNING, "journal.late_result_listener_failed", error=str(exc))
                return record

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

    # -- uninstall: accepted ≠ finished (P2) --------------------------------
    async def settle_started(self, command_id: str, *, ack_timeout_ms: int) -> bool:
        """Move an **uninstall** command to ``running`` (accepted, not finished).

        Before P2 the handler's ``{started: true}`` result went through the normal
        settlement path, so the console showed ``ok`` seconds after the console
        click — while the purge helper was still working, and sometimes while it
        had already failed. ``running`` is the honest state for that window, and
        it also sets ``expires_at_ms`` to the ACK deadline so housekeeping can
        flag a missing confirmation later.

        Returns ``True`` when this call performed the transition; ``False`` means
        the row is not an open uninstall (caller settles it normally).
        """

        now = self._clock.now_ms()
        async with self._sessionmaker() as session:
            outcome = await session.execute(
                update(Command)
                .where(
                    Command.id == str(command_id),
                    Command.type == "uninstall",
                    Command.status.in_(("pending", "dispatched")),
                )
                .values(status="running", expires_at_ms=now + max(int(ack_timeout_ms), 1_000))
            )
            await session.commit()
            changed = outcome.rowcount == 1
        if changed:
            log_event(log, logging.INFO, "journal.started", command_id=command_id)
        return changed

    async def settle_ack(
        self, command_id: str, *, result: dict[str, Any] | None = None
    ) -> tuple[CommandRecord | None, bool]:
        """Settle an uninstall command from a **persisted ACK** (the only proof).

        Returns ``(record, changed)``. ``changed`` is ``True`` only when *this*
        call performed the transition, so an idempotent retry can answer "already
        confirmed" instead of claiming a second success.

        Accepts ``running`` (normal), ``unconfirmed`` (the ACK arrived after the
        deadline — new evidence, so it upgrades the row and is logged), and the
        two pre-running states (the ACK can overtake the ``started`` result when
        the two uplinks are reordered).
        """

        now = self._clock.now_ms()
        async with self._sessionmaker() as session:
            previous = (
                await session.execute(select(Command.status).where(Command.id == str(command_id)))
            ).scalar()
            if previous is None:
                log_event(log, logging.WARNING, "journal.ack_missing", command_id=command_id)
                return None, False
            outcome = await session.execute(
                update(Command)
                .where(
                    Command.id == str(command_id),
                    Command.type == "uninstall",
                    Command.status.in_(("pending", "dispatched", "running", "unconfirmed")),
                )
                .values(
                    status="ok",
                    result=dict(result) if isinstance(result, dict) else {"acked": True},
                    error=None,
                    error_code=None,
                    finished_at_ms=now,
                )
            )
            await session.commit()
            row = (await session.execute(select(Command).where(Command.id == str(command_id)))).scalars().first()
            if row is None:
                return None, False
            record = CommandRecord.from_model(row)
            changed = outcome.rowcount == 1
            if not changed:
                log_event(
                    log,
                    logging.DEBUG,
                    "journal.ack_ignored",
                    command_id=command_id,
                    status=record.status,
                )
                return record, False
        if previous == "unconfirmed":
            log_event(log, logging.WARNING, "journal.ack_after_deadline", command_id=command_id)
        slot = self._waiters.get(command_id)
        if slot is not None:
            slot.settle(
                Result(
                    ok=True,
                    status="ok",
                    result=record.result,
                    error=None,
                    error_code=None,
                    retryable=False,
                    command_id=command_id,
                    deadline_ms=record.deadline_ms,
                    server_time_ms=now,
                    latency_ms=max(0, now - record.created_at_ms),
                )
            )
        return record, True

    async def open_uninstall(self, device_id: int) -> CommandRecord | None:
        """The single outstanding uninstall command for *device*, if any.

        P2 invariant: at most one open uninstall per device, otherwise a legacy
        ACK without a command id could settle several rows at once.
        """

        statement = (
            select(Command)
            .where(
                Command.device_id == int(device_id),
                Command.type == "uninstall",
                Command.status.in_(OPEN_COMMAND_STATUSES),
            )
            .order_by(Command.created_at_ms.desc())
            .limit(1)
        )
        async with self._sessionmaker() as session:
            row = (await session.execute(statement)).scalars().first()
        return CommandRecord.from_model(row) if row is not None else None

    async def expire_uninstall_acks(self, *, now: int | None = None) -> int:
        """``running`` uninstall past its ACK deadline → ``unconfirmed``.

        Deliberately **not** ``failed``: a missing ACK cannot distinguish "the
        device finished but the ack was lost" from "the purge never ran", so the
        row records "we do not know" (C6: observable failure, not a guess) and the
        device record is left in place for an operator to resolve.
        """

        moment = now if now is not None else self._clock.now_ms()
        total = 0
        async with self._sessionmaker() as session:
            while True:
                rows = list(
                    (
                        await session.execute(
                            select(Command.id)
                            .where(
                                Command.type == "uninstall",
                                Command.status == "running",
                                Command.expires_at_ms > 0,
                                Command.expires_at_ms < moment,
                            )
                            .order_by(Command.expires_at_ms.asc())
                            .limit(EXPIRE_BATCH)
                        )
                    ).scalars().all()
                )
                if not rows:
                    break
                outcome = await session.execute(
                    update(Command)
                    .where(Command.id.in_(rows), Command.status == "running")
                    .values(
                        status="unconfirmed",
                        error="卸载回执超时，执行结果无法确认（设备可能已卸载，也可能未执行）",
                        error_code=ACK_TIMEOUT,
                        finished_at_ms=moment,
                    )
                )
                await session.commit()
                total += int(outcome.rowcount or 0)
                if len(rows) < EXPIRE_BATCH:
                    break
        if total:
            log_event(log, logging.WARNING, "journal.ack_timeout", count=total)
        return total

    async def expire_stale(self, *, now: int | None = None) -> int:
        """Mark commands past their expiry as timeout; settles any waiter.

        F20: one conditional UPDATE per batch (rowcount = actually expired, no
        read-modify-write race) and a LIMIT so a huge backlog cannot turn one
        housekeeping tick into an unbounded transaction.
        """

        moment = now if now is not None else self._clock.now_ms()
        expired: list[str] = []
        async with self._sessionmaker() as session:
            while True:
                rows = list(
                    (
                        await session.execute(
                            select(Command.id)
                            .where(
                                Command.status.in_(("pending", "dispatched")),
                                Command.expires_at_ms > 0,
                                Command.expires_at_ms < moment,
                            )
                            .order_by(Command.expires_at_ms.asc())
                            .limit(EXPIRE_BATCH)
                        )
                    ).scalars().all()
                )
                if not rows:
                    break
                outcome = await session.execute(
                    update(Command)
                    .where(Command.id.in_(rows), Command.status.in_(("pending", "dispatched")))
                    .values(
                        status="timeout",
                        error="命令已过期（受控端未在期限内响应）",
                        error_code="TIMEOUT",
                        finished_at_ms=moment,
                    )
                )
                await session.commit()
                expired.extend(rows[: outcome.rowcount] if outcome.rowcount >= 0 else rows)
                if len(rows) < EXPIRE_BATCH:
                    break
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
    "ACK_TIMEOUT",
    "CommandJournal",
    "CommandRecord",
    "MAX_REPLAY_ATTEMPTS",
    "OPEN_COMMAND_STATUSES",
    "REPLAY_BACKOFF_BASE_MS",
    "REPLAY_BACKOFF_MAX_MS",
    "REPLAY_EXHAUSTED",
    "TERMINAL_COMMAND_STATUSES",
    "UNINSTALL_FAILED",
    "WaiterSlot",
    "replay_backoff_ms",
]