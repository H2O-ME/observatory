"""The single device-authentication path (P1).

Before 0.4.0 two modules answered "which device does this token belong to?" with
their own query: the link directory (:mod:`observatory.link.registry`) and the
frame-ingress dependency (:mod:`observatory.console.agent_frames`) — and each of
them *also* consulted the delete-tombstone table. Two copies of a security
decision drift apart, so the query now lives here exactly once.

Semantics
---------
* ``X-Device-Token`` is the credential; ``devices.uid`` is only an identifier and
  never authenticates anything on its own.
* A token matches the **current** ``devices.token_hash``, or — within
  ``settings.token_rotate_grace_s`` — a row in ``device_token_history`` written
  when that token was superseded. The grace window covers the "the server
  committed the rotation but the response was lost" retry; without it a retried
  enrolment would lock the device out.
* A device whose row says ``uninstalled_at_ms`` is terminal: ``GONE`` (410), so
  the agent stops reconnecting instead of spinning on 401 → re-enrol → 401.
* Anything else returns ``None``; callers map that to 401. There is deliberately
  **no ban list**: a deleted device has no row and (because the history table
  cascades) no token history, so its old token is rejected by the same ``None``
  path as any other unknown token. Deletion forgets; it does not forbid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .clock import now_ms
from .crypto import device_token_hash
from .errors import AppError
from .models import Device, DeviceTokenHistory

#: ``match_credential`` results, richest first.
MATCH_CURRENT = "current"
MATCH_HISTORY = "history"
MATCH_NONE = ""


@dataclass(frozen=True, slots=True)
class ResolvedDevice:
    """Detached snapshot of an authenticated device.

    Carriers keep it after the session closes, so it must not hold a lazy ORM
    instance (accessing one after the session is gone would raise).
    """

    device_id: int
    uid: str
    token_hash: str
    protocol: int = 3
    agent_version: str = ""
    boot_id: str = ""
    settings: Mapping[str, Any] = field(default_factory=dict)
    matched: str = MATCH_CURRENT


def _digest(token: str, secret_key: str) -> str:
    return device_token_hash(token, secret_key)


def _grace_ms(settings: Any) -> int:
    try:
        return max(0, int(getattr(settings, "token_rotate_grace_s", 0) or 0)) * 1000
    except (TypeError, ValueError):
        return 0


async def _history_device(session: AsyncSession, digest: str, *, settings: Any) -> Device | None:
    """The device whose *superseded* token hashes to *digest*, while in grace."""

    grace = _grace_ms(settings)
    if grace <= 0:
        return None
    cutoff = now_ms() - grace
    row = (
        await session.execute(
            select(Device)
            .join(DeviceTokenHistory, DeviceTokenHistory.device_id == Device.id)
            .where(
                DeviceTokenHistory.token_hash == digest,
                DeviceTokenHistory.superseded_at_ms >= cutoff,
            )
        )
    ).scalars().first()
    return row


async def load_device(
    session: AsyncSession,
    token: str,
    *,
    settings: Any,
    allow_uninstalled: bool = False,
) -> Device | None:
    """Resolve *token* to the live ORM row, or ``None`` when it is unknown.

    Raises :class:`AppError` ``GONE`` (410) for an uninstalled device: that is a
    terminal answer the agent must not retry. Callers that deliberately handle
    uninstalled rows pass ``allow_uninstalled=True`` — the uninstall ACK endpoint
    has to keep authenticating the device *after* the row is marked uninstalled,
    otherwise its own retry (the response may have been lost) would come back 410
    and the agent would treat a successful purge as a dead link.
    """

    cleaned = (token or "").strip()
    if not cleaned:
        return None
    digest = _digest(cleaned, settings.secret_key)
    device = (
        await session.execute(
            select(Device).where(Device.token_hash == digest, Device.token_hash != "")
        )
    ).scalars().first()
    if device is None:
        device = await _history_device(session, digest, settings=settings)
    if device is None:
        return None
    if device.uninstalled_at_ms and not allow_uninstalled:
        raise AppError.gone("受控端已卸载，旧令牌不再可用")
    return device


async def resolve_device(session: AsyncSession, token: str, *, settings: Any) -> ResolvedDevice | None:
    """:func:`load_device` projected into a detached snapshot (carrier use)."""

    device = await load_device(session, token, settings=settings)
    if device is None:
        return None
    return ResolvedDevice(
        device_id=int(device.id),
        uid=str(device.uid or ""),
        token_hash=str(device.token_hash or ""),
        protocol=int(device.protocol or 3),
        agent_version=str(device.agent_version or ""),
        boot_id=str(device.boot_id or ""),
        settings=dict(device.settings or {}),
    )


async def match_credential(
    session: AsyncSession,
    device: Device,
    token: str,
    *,
    settings: Any,
) -> str:
    """How *token* proves ownership of *device*: ``current``, ``history`` or ``""``.

    Read-only and non-raising: enrolment needs to know whether a request carries
    proof *before* deciding between "rotate" and "reject", and an uninstalled row
    must still be able to prove prior ownership.
    """

    cleaned = (token or "").strip()
    if not cleaned or device is None:
        return MATCH_NONE
    digest = _digest(cleaned, settings.secret_key)
    if digest and digest == str(device.token_hash or ""):
        return MATCH_CURRENT
    grace = _grace_ms(settings)
    if grace <= 0:
        return MATCH_NONE
    cutoff = now_ms() - grace
    row = (
        await session.execute(
            select(DeviceTokenHistory.id).where(
                DeviceTokenHistory.device_id == int(device.id),
                DeviceTokenHistory.token_hash == digest,
                DeviceTokenHistory.superseded_at_ms >= cutoff,
            )
        )
    ).scalars().first()
    return MATCH_HISTORY if row is not None else MATCH_NONE


__all__ = [
    "MATCH_CURRENT",
    "MATCH_HISTORY",
    "MATCH_NONE",
    "ResolvedDevice",
    "load_device",
    "match_credential",
    "resolve_device",
]
