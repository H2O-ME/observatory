"""Command creation, dispatch and settlement.

A command is the only way the server asks a device to do something. It is always
persisted first, so a device that is currently polling (or briefly offline) still
receives it later, and the console can show its real status instead of pretending
the action succeeded.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Command, Device, now_ms
from . import events
from .bus import CommandBus

#: Every command type the server will ever send. This is the enforcement point
#: for the project's stated capability boundary — nothing outside this set can
#: reach a device.
ALLOWED_COMMAND_TYPES: frozenset[str] = frozenset(
    {
        "screenshot",
        "collect_now",
        "sysinfo",
        "files",
        "log_tail",
        "update",
        "reload_config",
        "restart_agent",
        "uninstall",
        "reboot",
        "poweroff",
    }
)

#: Types that change the machine or remove the agent; the console must confirm.
CONFIRM_REQUIRED: frozenset[str] = frozenset({"uninstall", "restart_agent", "reboot", "poweroff"})

DEFAULT_TIMEOUT_MS = 30_000
COMMAND_TIMEOUTS_MS: dict[str, int] = {
    "files": 90_000,
    "update": 60_000,
    "uninstall": 60_000,
    "sysinfo": 60_000,
    "log_tail": 45_000,
}


class CommandError(ValueError):
    pass


def timeout_for(command_type: str) -> int:
    return COMMAND_TIMEOUTS_MS.get(command_type, DEFAULT_TIMEOUT_MS)


def validate_payload(command_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Reject malformed payloads before they touch the database or a device."""

    if command_type not in ALLOWED_COMMAND_TYPES:
        raise CommandError(f"不允许的命令类型: {command_type}")
    payload = dict(payload or {})

    if command_type == "files":
        op = payload.get("op")
        allowed_ops = {
            "list",
            "stat",
            "mkdir",
            "delete",
            "rename",
            "copy",
            "move",
            "search",
            "drives",
            "read",
            "write",
            "hash",
        }
        if op not in allowed_ops:
            raise CommandError(f"不允许的文件操作: {op}")
        if op != "drives" and not payload.get("path") and not payload.get("handle"):
            raise CommandError("缺少路径")
    elif command_type == "update":
        version = payload.get("version")
        if version is not None and not isinstance(version, str):
            raise CommandError("version 必须是字符串")
    elif command_type == "log_tail":
        lines = payload.get("lines", 200)
        try:
            payload["lines"] = max(1, min(5000, int(lines)))
        except (TypeError, ValueError) as exc:
            raise CommandError("lines 必须是整数") from exc
    elif command_type in {"reboot", "poweroff", "restart_agent", "uninstall"}:
        # Nothing beyond the confirm flag; drop anything the caller smuggled in.
        payload = {}

    return payload


def serialize(command: Command) -> dict[str, Any]:
    return {
        "id": command.id,
        "type": command.type,
        "status": command.status,
        "payload": command.payload or {},
        "result": command.result,
        "error": command.error,
        "created_at_ms": command.created_at_ms,
        "dispatched_at_ms": command.dispatched_at_ms,
        "finished_at_ms": command.finished_at_ms,
    }


async def create(
    session: AsyncSession,
    *,
    device: Device,
    command_type: str,
    payload: dict[str, Any] | None,
    actor: str,
    bus: CommandBus,
    agent_hub: Any,
    console_hub: Any,
) -> Command:
    validated = validate_payload(command_type, payload)
    now = now_ms()
    command = Command(
        id=uuid.uuid4().hex,
        device_id=device.id,
        type=command_type,
        payload=validated,
        status="pending",
        actor=actor,
        created_at_ms=now,
        expires_at_ms=now + timeout_for(command_type) * 3,
    )
    session.add(command)
    await session.commit()
    await session.refresh(command)

    bus.open(command.id)
    pushed = await push(agent_hub, command)
    if pushed:
        command.status = "dispatched"
        command.dispatched_at_ms = now_ms()
        await session.commit()
    else:
        notify = getattr(agent_hub, "notify_command", None)
        if callable(notify):
            notify(device.id)

    await events.emit(
        console_hub,
        events.EVENT_COMMAND_CREATED,
        device_uid=device.uid,
        data={"command": serialize(command)},
    )
    return command


async def push(agent_hub: Any, command: Command) -> bool:
    """Deliver over the live WebSocket, if the device has one."""

    if agent_hub is None:
        return False
    return await agent_hub.send_json(
        command.device_id,
        {
            "t": "command",
            "command": {
                "id": command.id,
                "type": command.type,
                "payload": command.payload or {},
                "timeout_ms": timeout_for(command.type),
            },
        },
    )


async def claim_pending(session: AsyncSession, device_id: int, limit: int = 10) -> list[Command]:
    """Poll path: hand undelivered commands to the device and mark them sent."""

    result = await session.execute(
        select(Command)
        .where(Command.device_id == device_id, Command.status == "pending")
        .order_by(Command.created_at_ms.asc())
        .limit(limit)
    )
    commands = list(result.scalars().all())
    if not commands:
        return []
    now = now_ms()
    for command in commands:
        command.status = "dispatched"
        command.dispatched_at_ms = now
    await session.commit()
    return commands


async def record_result(
    session: AsyncSession,
    *,
    command_id: str,
    ok: bool,
    result: dict[str, Any] | None,
    error: str | None,
    bus: CommandBus,
    console_hub: Any,
    device_uid: str = "",
) -> Command | None:
    command = await session.get(Command, command_id)
    if command is None:
        return None
    if command.status in {"ok", "error", "timeout", "cancelled"}:
        return command

    status = "ok" if ok else "error"
    command.status = status
    command.result = result
    command.error = error
    command.finished_at_ms = now_ms()
    await session.commit()

    bus.settle(command_id, status, result=result, error=error)
    await events.emit(
        console_hub,
        events.EVENT_COMMAND_UPDATED,
        device_uid=device_uid,
        data={"command": serialize(command)},
    )
    return command


async def fail(
    session: AsyncSession,
    command: Command,
    *,
    status: str,
    error: str,
    bus: CommandBus,
    console_hub: Any,
    device_uid: str = "",
) -> None:
    if command.status in {"ok", "error", "timeout", "cancelled"}:
        return
    command.status = status
    command.error = error
    command.finished_at_ms = now_ms()
    await session.commit()
    bus.settle(command.id, status, error=error)
    await events.emit(
        console_hub,
        events.EVENT_COMMAND_UPDATED,
        device_uid=device_uid,
        data={"command": serialize(command)},
    )


async def expire_stale(session: AsyncSession, bus: CommandBus, console_hub: Any) -> int:
    """Mark commands whose deadline passed as timed out."""

    now = now_ms()
    result = await session.execute(
        select(Command).where(Command.status.in_(("pending", "dispatched")), Command.expires_at_ms > 0)
    )
    expired = 0
    for command in result.scalars().all():
        if command.expires_at_ms > now:
            continue
        command.status = "timeout"
        command.error = "受控端未在期限内响应"
        command.finished_at_ms = now
        bus.settle(command.id, "timeout", error=command.error)
        expired += 1
    if expired:
        await session.commit()
        await events.emit(console_hub, events.EVENT_NOTICE, data={"message": f"{expired} 条命令超时"})
    return expired


async def wait_for(
    session: AsyncSession,
    command: Command,
    *,
    bus: CommandBus,
    timeout_s: float,
    device_uid: str = "",
    console_hub: Any = None,
) -> dict[str, Any]:
    """Block until the device answers; returns a ``FileOpResult``-shaped dict."""

    from .bus import CommandTimeoutError

    try:
        waiter = await bus.wait(command.id, timeout_s)
    except CommandTimeoutError:
        await fail(
            session,
            command,
            status="timeout",
            error=f"受控端在 {timeout_s:.0f} 秒内没有响应",
            bus=bus,
            console_hub=console_hub,
            device_uid=device_uid,
        )
        return {"ok": False, "op": command.type, "data": {}, "error": "受控端响应超时", "elapsed_ms": 0}
    finally:
        bus.forget(command.id)
    payload = waiter.result if isinstance(waiter.result, dict) else {}
    # The agent answers file operations with a self-describing payload
    # ({"ok":..., "op":..., "data":...}); pass it through untouched.
    if "ok" in payload:
        return payload
    return {
        "ok": waiter.ok,
        "op": command.type,
        "data": payload,
        "error": waiter.error,
        "elapsed_ms": max(0, (command.finished_at_ms or now_ms()) - command.created_at_ms),
    }


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "ALLOWED_COMMAND_TYPES",
    "COMMAND_TIMEOUTS_MS",
    "CONFIRM_REQUIRED",
    "CommandError",
    "claim_pending",
    "create",
    "dumps",
    "expire_stale",
    "fail",
    "push",
    "record_result",
    "serialize",
    "timeout_for",
    "validate_payload",
    "wait_for",
]