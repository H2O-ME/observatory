"""DB-backed settings store for the 0.3.0 console.

``core.config`` owns the *schema* of every setting (``SPECS`` / ranges /
defaults); this module owns persistence and the small in-process cache.  There
is exactly one store per process, shared by the console, the legacy compatibility
layer and whatever media code needs the effective value of a retention knob.

Two views exist:

* :meth:`SettingsStore.effective` -- server truth, may contain ``SECRET_KEYS``;
  never serialize it directly;
* :func:`observatory.console.dto.device_safe_config` -- the only shape allowed to
  leave the process (A4).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.settings_spec import (
    DEFAULT_SETTINGS,
    PER_DEVICE_KEYS,
    SPECS,
    WRITABLE,
    SettingsError,
    normalize,
)
from ..core.models import Setting
from ..core.clock import now_ms

log = logging.getLogger(__name__)

__all__ = ["SettingsStore", "SettingsError", "get_store"]


class SettingsStore:
    """Cached view over the ``settings`` table."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    async def load(self, session: AsyncSession) -> dict[str, Any]:
        if self._cache is not None:
            return dict(self._cache)
        rows = (await session.execute(select(Setting))).scalars().all()
        values = dict(DEFAULT_SETTINGS)
        for row in rows:
            if row.key not in SPECS:
                continue
            try:
                values[row.key] = normalize(row.key, row.value)
            except SettingsError:
                # A hand-edited database must not take the console down.  The
                # declared default wins and the drift is logged, not swallowed.
                log.warning("ignoring invalid stored setting %s=%r", row.key, row.value)
        self._cache = values
        return dict(values)

    async def patch(self, session: AsyncSession, updates: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, raw in updates.items():
            if key not in WRITABLE:
                raise SettingsError(f"设置项不可写: {key}")
            normalized[key] = normalize(key, raw)

        for key, value in normalized.items():
            row = await session.get(Setting, key)
            if row is None:
                session.add(Setting(key=key, value=value, updated_at_ms=now_ms()))
            else:
                row.value = value
                row.updated_at_ms = now_ms()
        await session.commit()
        self.invalidate()
        return normalized

    async def effective(self, session: AsyncSession, device_settings: dict[str, Any] | None) -> dict[str, Any]:
        """Server defaults + stored values + per-device overrides (server truth)."""

        values = await self.load(session)
        for key, raw in (device_settings or {}).items():
            if key not in PER_DEVICE_KEYS:
                continue
            try:
                values[key] = normalize(key, raw)
            except SettingsError as exc:
                # Writes go through ``normalize`` before they reach the database,
                # so this can only be a hand-edited row: log and fall back to the
                # server default instead of failing every device read.
                log.warning("ignoring invalid device override %s=%r: %s", key, raw, exc)
        return values

    def invalidate(self) -> None:
        self._cache = None


_STORE: SettingsStore | None = None


def get_store() -> SettingsStore:
    global _STORE
    if _STORE is None:
        _STORE = SettingsStore()
    return _STORE