"""Console device endpoints.

Everything that describes a device is built from ``presence.snapshot`` (C4/A13)
and from ``device_safe_config`` (A4).  Commands are created exactly once, in
``link.journal`` (C3); this module only validates the HTTP payload and dispatches
the resulting record through the session that currently owns the device.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import presence
from ..core.settings_spec import SettingsError, normalize
from ..core.db import get_session
from ..core.errors import AppError
from ..core.models import Command, Device, Frame
from . import audit
from .deps import client_ip, get_journal, get_links, get_settings, require_admin, store
from .dto import budget_ms, command_out, device_out, device_state, stamp
from .envelope import ConsoleRoute
from .settings import push_config
from .updates import require_sha256

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/devices",
    tags=["devices"],
    dependencies=[Depends(require_admin)],
    route_class=ConsoleRoute,
)

ALLOWED_COMMAND_TYPES = frozenset(
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
CONFIRM_REQUIRED = frozenset({"uninstall", "restart_agent", "reboot", "poweroff"})
_FILE_OPS = frozenset(
    {"list", "stat", "mkdir", "delete", "rename", "copy", "move", "search", "drives", "read", "write", "hash"}
)
_PENDING_STATUSES = ("pending", "dispatched")


async def load_device(session: AsyncSession, uid: str) -> Device:
    device = (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()
    if device is None:
        raise AppError.gone("设备不存在")
    return device


async def uninstall_pending(session: AsyncSession, device_id: int) -> bool:
    kind_column = getattr(Command, "kind", None) or getattr(Command, "type", None)
    if kind_column is None:
        raise RuntimeError("Command model has neither kind nor type")
    result = await session.execute(
        select(Command.id).where(
            Command.device_id == device_id,
            kind_column == "uninstall",
            Command.status.in_(_PENDING_STATUSES),
        )
    )
    return result.first() is not None


async def latest_frame_ids(session: AsyncSession, device_ids: list[int]) -> dict[int, int]:
    if not device_ids:
        return {}
    result = await session.execute(
        select(Frame.device_id, func.max(Frame.id)).where(Frame.device_id.in_(device_ids)).group_by(Frame.device_id)
    )
    return {int(row[0]): int(row[1]) for row in result.fetchall() if row[1] is not None}


async def device_state_for(session: AsyncSession, device: Device) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(effective_settings, presence_state)`` for one device."""

    effective = await store().effective(session, device.settings or {})
    pending = await uninstall_pending(session, device.id)
    state = device_state(device, effective_settings=effective, uninstall_pending=pending)
    return effective, presence.snapshot(state)


async def device_payload(session: AsyncSession, device: Device, *, latest_frame_id: int | None = None) -> dict[str, Any]:
    effective, snapshot = await device_state_for(session, device)
    return device_out(
        device,
        state=snapshot,
        effective_settings=effective,
        latest_frame_id=latest_frame_id,
    )


def validate_command_payload(command_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    if command_type not in ALLOWED_COMMAND_TYPES:
        raise AppError.bad_request(f"不允许的命令类型: {command_type}")
    body = dict(payload or {})
    if command_type == "files":
        op = body.get("op")
        if op not in _FILE_OPS:
            raise AppError.bad_request(f"不允许的文件操作: {op}")
        if op != "drives" and not body.get("path") and not body.get("handle"):
            raise AppError.bad_request("缺少路径")
    elif command_type == "update":
        require_sha256(body)
        version = body.get("version")
        if version is not None and not isinstance(version, str):
            raise AppError.bad_request("version 必须是字符串")
    elif command_type == "log_tail":
        try:
            body["lines"] = max(1, min(5000, int(body.get("lines", 200))))
        except (TypeError, ValueError) as exc:
            raise AppError.bad_request("lines 必须是整数") from exc
    elif command_type in CONFIRM_REQUIRED:
        body = {}
    return body


async def create_command(
    request: Request,
    session: AsyncSession,
    device: Device,
    *,
    command_type: str,
    payload: dict[str, Any] | None,
    actor: str,
) -> Any:
    """Create exactly one journal record, then try to dispatch it.

    The record is persisted even when the device is unreachable: the legacy poll
    path and the new outbox both claim from the same journal, so "device offline"
    is a delivery detail, not a lost command.
    """

    body = validate_command_payload(command_type, payload)
    deadline_ms = budget_ms(command_type)
    record = await get_journal(request).create(
        device.id,
        command_type,
        body,
        actor=actor,
        timeout_ms=deadline_ms,
    )
    links = get_links(request)
    agent_session = links.get(device.id)
    if agent_session is not None:
        # Hand exactly this record to the live carrier. The carrier owns delivery:
        # the outbox/replay re-reads the journal, so the console must not claim
        # *other* pending rows here (that would hide offline commands from the
        # replay path). Status becomes ``ok``/``error`` when the agent reports the
        # result; until then ``pending`` is the honest state.
        try:
            await agent_session.send(
                "command",
                record.to_envelope_body(deadline_ms=deadline_ms),
                deadline_ms=deadline_ms,
            )
        except AppError as exc:
            log.warning("live dispatch failed uid=%s command=%s: %s", device.uid, record.id, exc.message)
    return record


@router.get("")
async def list_devices(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    devices = list((await session.execute(select(Device).order_by(Device.enrolled_at_ms.asc()))).scalars().all())
    frame_ids = await latest_frame_ids(session, [device.id for device in devices])
    payloads = [
        await device_payload(session, device, latest_frame_id=frame_ids.get(device.id)) for device in devices
    ]
    from ..core.config import resolve_version

    return stamp(
        {
            "devices": payloads,
            "server_version": resolve_version(),
            "channels": get_links(request).counts(),
        }
    )


@router.get("/{uid}")
async def get_device(
    uid: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await load_device(session, uid)
    frame_ids = await latest_frame_ids(session, [device.id])
    payload = await device_payload(session, device, latest_frame_id=frame_ids.get(device.id))
    return stamp({"device": payload})


@router.patch("/{uid}")
async def patch_device(
    uid: str,
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await load_device(session, uid)
    changes: dict[str, Any] = {}

    if payload.get("name") is not None:
        name = str(payload["name"]).strip()
        if len(name) > 120:
            raise AppError.bad_request("名称过长")
        device.name = name
        changes["name"] = name
    if payload.get("note") is not None:
        device.note = str(payload["note"]).strip()[:250]
        changes["note"] = device.note
    if payload.get("settings") is not None:
        submitted = payload["settings"]
        if not isinstance(submitted, dict):
            raise AppError.bad_request("settings 必须是对象")
        overrides = dict(device.settings or {})
        from ..core.config import PER_DEVICE_KEYS

        for key, value in submitted.items():
            if key not in PER_DEVICE_KEYS:
                raise AppError.bad_request(f"该设置不能按设备覆盖: {key}")
            try:
                overrides[key] = normalize(key, value)
            except SettingsError as exc:
                raise AppError.bad_request(str(exc)) from exc
        device.settings = overrides
        changes["settings"] = overrides

    await session.commit()
    await audit.record(
        session,
        action=audit.ACTION_DEVICE_UPDATE,
        device=device,
        ip=client_ip(request),
        detail=changes,
    )
    from .events import EVENT_DEVICE_UPDATED, emit

    await emit(EVENT_DEVICE_UPDATED, device_uid=device.uid, data={"changes": list(changes)})
    await push_config(session, links=get_links(request), device=device)

    frame_ids = await latest_frame_ids(session, [device.id])
    out = await device_payload(session, device, latest_frame_id=frame_ids.get(device.id))
    return stamp({"device": out})


@router.delete("/{uid}")
async def delete_device(
    uid: str,
    request: Request,
    purge_remote: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await load_device(session, uid)
    _effective, snapshot = await device_state_for(session, device)
    if snapshot.get("state") in {"online", "degraded", "uninstalling"}:
        raise AppError.bad_request("设备仍在线，请先远程卸载再删除记录")

    agents = get_links(request)
    await agents.unregister(device.id)
    await audit.record(
        session,
        action=audit.ACTION_DEVICE_DELETE,
        device=device,
        ip=client_ip(request),
        detail={"purge_remote": bool(purge_remote)},
    )
    try:
        from ..media import ingest as media_ingest
    except ImportError as exc:  # pragma: no cover - W0 skeleton guard
        raise AppError.internal("媒体层未就绪，拒绝在未清理画面数据的情况下删除设备") from exc
    media_result = await media_ingest.delete_device_data(session, device, await store().load(session))
    await session.delete(device)
    await session.commit()
    from .events import EVENT_DEVICE_REMOVED, emit

    await emit(EVENT_DEVICE_REMOVED, device_uid=uid, data={"purge_remote": bool(purge_remote)})
    archive_error = str(media_result.get("archive_error") or "")
    if archive_error:
        # The local tree and frame rows are gone, but the remote copy is not;
        # report it instead of claiming a clean delete (A14/C6).
        log.warning("device %s deleted with remote archive leftovers: %s", uid, archive_error)
    return stamp(
        {
            "ok": True,
            "message": "设备记录已删除",
            "media": media_result,
            "archive_error": archive_error or None,
        }
    )


@router.post("/{uid}/uninstall")
async def uninstall_device(
    uid: str,
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await load_device(session, uid)
    if not payload.get("confirm"):
        raise AppError.bad_request("需要确认才能真正卸载")
    if device.uninstalled_at_ms:
        raise AppError.bad_request("该设备已经卸载过")
    _effective, snapshot = await device_state_for(session, device)
    if not snapshot.get("capabilities", {}).get("uninstall"):
        raise AppError.bad_request(snapshot.get("reason") or "设备当前不在线，无法下发卸载指令")

    await audit.record(
        session,
        action=audit.ACTION_DEVICE_UNINSTALL,
        device=device,
        ip=client_ip(request),
        detail={"reason": "console request"},
    )
    record = await create_command(
        request,
        session,
        device,
        command_type="uninstall",
        payload={},
        actor="console",
    )
    return stamp(
        {
            "command_id": record.id,
            "status": record.status,
            "message": "卸载指令已下发，受控端将以 SYSTEM 权限自行清理，不会弹 UAC",
        },
        deadline=budget_ms("uninstall"),
    )


@router.post("/{uid}/commands")
async def create_device_command(
    uid: str,
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await load_device(session, uid)
    command_type = str(payload.get("type") or "")
    if command_type in CONFIRM_REQUIRED and not payload.get("confirm"):
        raise AppError.bad_request(f"{command_type} 需要显式确认")
    command_payload = payload.get("payload")
    if command_payload is not None and not isinstance(command_payload, dict):
        raise AppError.bad_request("payload 必须是对象")
    command_payload = validate_command_payload(command_type, command_payload)
    if command_type == "update":
        # F5: never dispatch an update that references a missing/mismatched
        # package or a version below the self-update floor.
        await _validate_update_target(request, session, command_payload)
    record = await create_command(
        request,
        session,
        device,
        command_type=command_type,
        payload=command_payload or {},
        actor="console",
    )
    await audit.record(
        session,
        action=audit.ACTION_COMMAND_CREATE,
        device=device,
        ip=client_ip(request),
        detail={"type": command_type, "command_id": record.id},
    )
    from .events import EVENT_COMMAND_CREATED, emit

    await emit(EVENT_COMMAND_CREATED, device_uid=device.uid, data={"command_id": record.id, "type": command_type})
    # F7: the envelope must carry the command budget so the browser never
    # times out earlier than the server when the global ceiling is raised.
    return {**command_out(record), "deadline_ms": budget_ms(command_type)}


async def _validate_update_target(request: Request, session: AsyncSession, body: dict[str, Any]) -> None:
    """F5: a manual update must reference a real, hash-matched, updatable package."""

    from ..core.config import MIN_SELF_UPDATE_VERSION, parse_version
    from .updates import PackageService

    version = str(body.get("version") or "")
    sha256 = str(body.get("sha256") or "").strip().lower()
    if not version:
        raise AppError.bad_request("update 命令缺少 version")
    try:
        parse_version(version)
    except ValueError as exc:
        raise AppError.bad_request("version 格式错误") from exc
    if parse_version(version) < parse_version(MIN_SELF_UPDATE_VERSION):
        raise AppError.bad_request(f"目标版本低于最低可自更新版本 {MIN_SELF_UPDATE_VERSION}")
    package = next(
        (item for item in await PackageService(get_settings(request)).list(session, "agent") if item.version == version),
        None,
    )
    if package is None:
        raise AppError.bad_request(f"没有 {version} 版本的安装包，请先在「更新」页上传")
    if not package.sha256 or sha256 != str(package.sha256).lower():
        raise AppError.bad_request("sha256 与该安装包不一致")


@router.get("/{uid}/commands")
async def list_commands(
    uid: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await load_device(session, uid)
    commands = await get_journal(request).list(device.id, limit=limit, offset=offset)
    return stamp({"commands": [command_out(command) for command in commands]})


@router.get("/{uid}/commands/{command_id}")
async def get_command(
    uid: str,
    command_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await load_device(session, uid)
    command = await get_journal(request).get(command_id)
    if command is None or int(command.device_id) != device.id:
        raise AppError.gone("命令不存在")
    return command_out(command)


__all__ = [
    "ALLOWED_COMMAND_TYPES",
    "CONFIRM_REQUIRED",
    "create_command",
    "device_payload",
    "device_state_for",
    "latest_frame_ids",
    "load_device",
    "router",
    "uninstall_pending",
    "validate_command_payload",
]