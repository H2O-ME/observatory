"""Carrier-independent downlink queue with at-least-once delivery.

A message stays queued until the agent acknowledges it through ``last_ack_id``
(HTTP carrier) or by id (WS carrier). Both carriers consume the same queue, so
switching carrier never changes ordering or delivery semantics (C1).

The queue is bounded on purpose: silently dropping a command would violate C6,
so ``put`` raises an observable error instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from ..core.errors import AppError
from ..core.log import log_event
from .envelope import Envelope

log = logging.getLogger(__name__)


class Outbox:
    """Bounded FIFO of downlink envelopes plus an ack watermark."""

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = max(1, int(maxsize))
        self._pending: OrderedDict[str, Envelope] = OrderedDict()
        self._event = asyncio.Event()
        self._last_ack_id: str | None = None
        self._closed = False
        #: Monotonic change counter (0.3.1 F22 final design): put/ack/wake/close
        #: each bump it. Carriers snapshot the revision *before* reading the
        #: queue and pass it to ``wait_for_change(since=...)``, so a change that
        #: lands between "check" and "clear" can never be slept away — while
        #: un-acked backlog deliberately does NOT count as a change (the pump
        #: must sleep instead of hot-looping re-sends; that mistake starved a
        #: fresh ctrl_stop behind thousands of duplicate writes on CI).
        self._revision = 0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_ack_id(self) -> str | None:
        return self._last_ack_id

    @property
    def revision(self) -> int:
        """Monotonic change counter for ``wait_for_change(since=...)`` (F22)."""

        return self._revision

    def _notify(self) -> None:
        self._revision += 1
        self._event.set()

    def pending_count(self) -> int:
        return len(self._pending)

    def pending(self, limit: int | None = None) -> list[Envelope]:
        values = list(self._pending.values())
        if limit is not None:
            return values[: max(1, limit)]
        return values

    def put(self, envelope: Envelope) -> None:
        if self._closed:
            raise AppError.gone("链路已关闭，无法下发")
        if envelope.id in self._pending:
            return
        if len(self._pending) >= self._maxsize:
            raise AppError.internal("下行队列已满，请稍后重试")
        self._pending[envelope.id] = envelope
        self._notify()

    def ack(self, message_id: str) -> bool:
        """Mark one message as delivered. Returns ``True`` when it was queued."""

        if not message_id:
            return False
        removed = self._pending.pop(message_id, None) is not None
        if removed:
            self._last_ack_id = message_id
            self._notify()
            log_event(log, logging.DEBUG, "outbox.acked", message_id=message_id)
        return removed

    def ack_up_to(self, message_id: str) -> int:
        """Legacy/HTTP watermark ack: drop everything up to and including id."""

        if not message_id or message_id not in self._pending:
            return 0
        removed = 0
        for queued_id in list(self._pending):
            self._pending.pop(queued_id, None)
            removed += 1
            self._last_ack_id = queued_id
            if queued_id == message_id:
                break
        if removed:
            self._notify()
            log_event(log, logging.DEBUG, "outbox.ack_watermark", message_id=message_id, removed=removed)
        return removed

    async def wait_for_messages(self, timeout_s: float) -> list[Envelope]:
        """Return pending messages, waiting at most *timeout_s* for a wake-up."""

        messages = self.pending()
        if messages or self._closed:
            return messages
        marker = self._revision
        # Clear *before* re-checking: a put that lands between the check above
        # and this clear would otherwise have its wake-up erased (F22).
        self._event.clear()
        if self._revision != marker:
            return self.pending()
        try:
            await asyncio.wait_for(self._event.wait(), timeout=max(0.01, float(timeout_s)))
        except asyncio.TimeoutError:
            # Not an error: the HTTP carrier simply ends its hold window.
            return self.pending()
        finally:
            self._event.clear()
        return self.pending()

    async def wait_for_change(self, timeout_s: float, *, since: int | None = None) -> bool:
        """Wait until the queue changes or *timeout_s* passes.

        Returns ``True`` when a put/ack/close woke the caller. ``since`` is the
        :attr:`revision` the caller already acted on; passing it makes the wait
        race-free — a put that lands between the caller's last check and the
        ``clear()`` bumps the revision, so it can never be slept away (F22).

        Note: un-acked backlog is deliberately **not** a change. Entries stay in
        ``pending()`` until the agent acks them, so a carrier that already wrote
        them must be able to sleep instead of hot-looping duplicate re-sends
        (that mistake starved a fresh ctrl_stop behind thousands of duplicate
        writes on slow CI runners — 2026-10-25 incident).
        """

        if self._closed:
            return True
        marker = self._revision if since is None else int(since)
        self._event.clear()
        if self._revision != marker:
            return True
        try:
            await asyncio.wait_for(self._event.wait(), timeout=max(0.01, float(timeout_s)))
        except asyncio.TimeoutError:
            return False
        finally:
            self._event.clear()
        return True

    def wake(self) -> None:
        """Wake a waiting carrier (used for pings/shutdown)."""

        self._notify()

    def close(self) -> None:
        self._closed = True
        self._pending.clear()
        self._notify()


__all__ = ["Outbox"]