"""Per-device wake-up for the HTTP long-poll transport.

Without this the fallback transport degrades into 1-2 second busy polling: the
agent asks "any commands?", gets nothing, asks again. An event per device means a
poll can block for the full hold window and wake the instant a command exists,
which is what makes the polling fallback feel immediate without burning CPU.
"""

from __future__ import annotations

import asyncio


class DeviceNotifier:
    def __init__(self) -> None:
        self._events: dict[int, asyncio.Event] = {}

    def event(self, device_id: int) -> asyncio.Event:
        event = self._events.get(device_id)
        if event is None:
            event = asyncio.Event()
            self._events[device_id] = event
        return event

    def notify(self, device_id: int) -> None:
        self.event(device_id).set()

    def reset(self, device_id: int) -> None:
        event = self._events.get(device_id)
        if event is not None:
            event.clear()

    async def wait(self, device_id: int, timeout_s: float) -> bool:
        """Wait for a wake-up. Returns True when notified, False on timeout."""

        event = self.event(device_id)
        if event.is_set():
            event.clear()
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.1, timeout_s))
        except asyncio.TimeoutError:
            return False
        event.clear()
        return True

    def forget(self, device_id: int) -> None:
        self._events.pop(device_id, None)

    def clear(self) -> None:
        self._events.clear()


__all__ = ["DeviceNotifier"]