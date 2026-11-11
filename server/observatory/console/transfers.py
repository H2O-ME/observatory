"""Server-side staged file retrieval ("取回") — v11.

Why this exists: the browser used to pull a device file itself, chunk by chunk
(``/devices/{uid}/files/raw`` → Blob → ``<a download>``).  That made the browser
tab the only copy of the transfer state: closing the console, leaving the page
or switching devices killed it.  Retrieval is now a **server-side background
task**: the server reads the file from the agent into ``transfers_dir`` and any
browser may download it from the server for a fixed 24 h.

The device read reuses the existing ``files`` command ``op:"read"`` (1 MiB
chunks, base64) — no agent change, no version bump.

Lifecycle::

    pending ──► running ──► ready   (expires_at_ms = completed + 24h)
        └────────┴───────► failed  (error explains why; retry = new transfer)
                        └─► cancelled (operator deleted it)

Only ``ready`` rows have a retention clock.  ``failed``/``cancelled`` rows are
kept so the page can show what happened, then swept after the same 24 h.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.db import get_session
from ..core.errors import AppError
from ..core.models import Device, FileTransfer
from . import audit
from .deps import client_ip, require_admin, store
from .devices import load_device
from .dto import budget_ms, stamp
from .envelope import ConsoleRoute
from .files import MAX_CHUNK_BYTES, _unwrap_agent_payload, ensure_reachable

log = logging.getLogger(__name__)

router = APIRouter(tags=["transfers"], dependencies=[Depends(require_admin)], route_class=ConsoleRoute)

#: Fixed retention: the operator asked for exactly 24 h, no settings key.
RETENTION_MS = 24 * 60 * 60 * 1000
#: Hard ceiling for one staged file when the device setting is missing/absurd.
DEFAULT_SIZE_LIMIT_BYTES = 512 * 1024 * 1024
#: Whole-spool ceiling (pending + running + ready).  Keeps N × 512 MiB from
#: filling the server disk; the per-file limit alone cannot do that.
TOTAL_QUOTA_BYTES = 2 * 1024 * 1024 * 1024
#: Never start a retrieval that would leave the data disk this close to full.
DISK_RESERVE_BYTES = 512 * 1024 * 1024
#: How many expired rows one housekeeping tick removes (single SQLite writer).
EXPIRE_BATCH = 200

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)
#: Rows whose bytes still count against :data:`TOTAL_QUOTA_BYTES`.
QUOTA_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_READY)


class TransferFailed(Exception):
    """A retrieval failure with a message meant for the operator."""


def _tasks(app: Any) -> dict[int, asyncio.Task[Any]]:
    """Strong references to running retrievals (event loop holds only weak refs)."""

    tasks = getattr(app.state, "file_transfer_tasks", None)
    if tasks is None:
        tasks = {}
        app.state.file_transfer_tasks = tasks
    return tasks


def _size_limit_bytes(effective: dict[str, Any]) -> int:
    raw = int(effective.get("file_upload_max_mb", 512) or 512)
    return max(1, raw) * 1024 * 1024


def _basename(path: str) -> str:
    name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
    return name[:255] or "download.bin"


def _storage_name(filename: str) -> str:
    """A uuid filename — the device path must never address our filesystem."""

    suffix = Path(filename).suffix.lower()
    if len(suffix) > 16 or not suffix[1:].isalnum():
        suffix = ""
    return f"{uuid.uuid4().hex}{suffix}"


def _final_path(settings: Any, storage_name: str) -> Path:
    return settings.transfers_dir / storage_name


def _part_path(settings: Any, storage_name: str) -> Path:
    return settings.transfers_dir / f"{storage_name}.part"


def _unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        path.unlink()


def _download_name(row: FileTransfer) -> str:
    name = (row.filename or "").replace("\r", "").replace("\n", "").strip()
    return name or f"transfer-{row.id}.bin"


def transfer_out(row: FileTransfer) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "device_uid": row.device_uid,
        "device_name": row.device_name,
        "path": row.remote_path,
        "name": row.filename,
        "expected_bytes": int(row.expected_bytes or 0),
        "received_bytes": int(row.received_bytes or 0),
        "status": row.status,
        "error": row.error,
        "sha256": row.sha256 or "",
        "created_at_ms": int(row.created_at_ms or 0),
        "completed_at_ms": row.completed_at_ms,
        "expires_at_ms": row.expires_at_ms,
        "content_url": (
            f"/api/v1/file-transfers/{int(row.id)}/content" if row.status == STATUS_READY else None
        ),
    }


# -- background retrieval ---------------------------------------------------


async def _patch(database: Any, transfer_id: int, **fields: Any) -> bool:
    """Update one row in its own short transaction; False when it vanished."""

    async with database.sessionmaker() as session:
        row = await session.get(FileTransfer, transfer_id)
        if row is None:
            return False
        for key, value in fields.items():
            setattr(row, key, value)
        await session.commit()
        return True


def _result_error_message(result: Any) -> str:
    message = str(getattr(result, "error", "") or "受控端读取失败")
    code = str(getattr(result, "error_code", "") or "")
    return f"{message}（{code}）" if code else message


async def _record_audit(app: Any, transfer_id: int, action: str, detail: dict[str, Any]) -> None:
    database = app.state.db
    if database is None:
        return
    try:
        async with database.sessionmaker() as session:
            row = await session.get(FileTransfer, transfer_id)
            if row is None:
                return
            await audit.record(
                session,
                action=action,
                actor="console",
                device_uid=row.device_uid,
                device_name=row.device_name,
                detail={"transfer_id": transfer_id, **detail},
            )
    except Exception as exc:  # noqa: BLE001 - audit must never change the outcome
        log.warning("file transfer audit failed (%s): %s", action, exc)


async def _fail(database: Any, transfer_id: int, message: str, part: Path | None = None) -> None:
    """Mark failed. Any ``.part`` is removed *before* the status commit.

    Order matters for the operator: once the page shows ``failed`` there must be
    no half file left on disk (the same "commit first for rows, but never advertise
    a state the bytes do not match" rule used by the retention sweeper).
    """

    if part is not None:
        _unlink(part)
    await _patch(
        database,
        transfer_id,
        status=STATUS_FAILED,
        error=message[:1000],
        completed_at_ms=now_ms(),
        expires_at_ms=None,
    )


async def run_transfer(app: Any, transfer_id: int) -> None:
    """Pull one file from its device into the spool, then mark it ready."""

    settings = app.state.settings
    database = app.state.db
    if database is None:
        return
    device_id: int | None = None
    missing_device = False
    async with database.sessionmaker() as session:
        row = await session.get(FileTransfer, transfer_id)
        if row is None or row.status not in (STATUS_PENDING, STATUS_RUNNING):
            return
        uid = row.device_uid
        storage_name = row.storage_name
        remote_path = row.remote_path
        expected_hint = int(row.expected_bytes or 0)
        device = (
            (await session.execute(select(Device).where(Device.uid == uid))).scalars().first()
        )
        if device is None:
            missing_device = True
            limit_bytes = DEFAULT_SIZE_LIMIT_BYTES
        else:
            device_id = device.id
            limit_bytes = _size_limit_bytes(await store().effective(session, device.settings or {}))

    part = _part_path(settings, storage_name)
    final = _final_path(settings, storage_name)
    if missing_device:
        await _fail(database, transfer_id, "设备记录已不存在", part)
        return
    if expected_hint > limit_bytes:
        await _fail(database, transfer_id, "文件超过该设备允许的取回上限", part)
        return

    links = getattr(app.state, "links", None)
    agent_session = links.get(device_id) if links is not None and device_id is not None else None
    if agent_session is None:
        await _fail(database, transfer_id, "设备当前没有活动链路", part)
        return

    part.parent.mkdir(parents=True, exist_ok=True)
    await _patch(database, transfer_id, status=STATUS_RUNNING, error=None)

    digest = hashlib.sha256()
    offset = 0
    expected = expected_hint
    try:
        with part.open("wb") as handle:
            while True:
                result = await agent_session.request(
                    "files",
                    {
                        "op": "read",
                        "path": remote_path,
                        "offset": offset,
                        "length": MAX_CHUNK_BYTES,
                    },
                    timeout_s=budget_ms("files") / 1000,
                )
                if not getattr(result, "ok", False):
                    raise TransferFailed(_result_error_message(result))
                data = _unwrap_agent_payload(getattr(result, "result", None))
                if not isinstance(data, dict):
                    raise TransferFailed("受控端返回的数据结构异常")
                try:
                    chunk = base64.b64decode(str(data.get("data") or ""))
                except (ValueError, TypeError) as exc:
                    raise TransferFailed("受控端返回的数据无法解码") from exc

                reported = int(data.get("size") or 0)
                if reported > 0 and reported != expected:
                    if reported > limit_bytes:
                        raise TransferFailed("文件超过该设备允许的取回上限")
                    expected = reported
                if chunk:
                    if offset + len(chunk) > limit_bytes:
                        raise TransferFailed("文件超过该设备允许的取回上限")
                    handle.write(chunk)
                    digest.update(chunk)
                    offset += len(chunk)
                    await _patch(database, transfer_id, received_bytes=offset, expected_bytes=expected)

                if data.get("eof") or not chunk:
                    break
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(part, final)
        completed = now_ms()
        await _patch(
            database,
            transfer_id,
            status=STATUS_READY,
            received_bytes=offset,
            expected_bytes=expected or offset,
            sha256=digest.hexdigest(),
            completed_at_ms=completed,
            expires_at_ms=completed + RETENTION_MS,
            error=None,
        )
        await _record_audit(
            app,
            transfer_id,
            audit.ACTION_FILE_TRANSFER_READY,
            {"path": remote_path, "bytes": offset, "sha256": digest.hexdigest()},
        )
    except asyncio.CancelledError:
        raise
    except TransferFailed as exc:
        await _fail(database, transfer_id, str(exc), part)
        await _record_audit(app, transfer_id, audit.ACTION_FILE_TRANSFER_FAILED, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - record the failure, never crash the loop
        log.warning("file transfer %s failed: %s", transfer_id, exc)
        await _fail(database, transfer_id, f"取回失败：{exc}", part)
        await _record_audit(app, transfer_id, audit.ACTION_FILE_TRANSFER_FAILED, {"error": str(exc)})
    finally:
        # Interrupted/failed: only the .part exists. Ready already renamed it.
        _unlink(_part_path(settings, storage_name))


# -- API --------------------------------------------------------------------


@router.post("/devices/{uid}/files/transfers")
async def create_transfer(
    uid: str,
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await load_device(session, uid)
    remote_path = str(payload.get("path") or "").strip()
    if not remote_path:
        raise AppError.bad_request("缺少文件路径")
    try:
        size = int(payload.get("size") or 0)
    except (TypeError, ValueError) as exc:
        raise AppError.bad_request("文件大小不合法") from exc
    if size < 0:
        raise AppError.bad_request("文件大小不合法")

    # Only the *start* needs a live device; the task runs on its own afterwards.
    await ensure_reachable(request, session, device)
    effective = await store().effective(session, device.settings or {})
    limit_bytes = _size_limit_bytes(effective)
    if size > limit_bytes:
        raise AppError.bad_request("文件超过该设备允许的取回上限")

    settings = request.app.state.settings
    now = now_ms()
    active_total = await session.scalar(
        select(func.coalesce(func.sum(FileTransfer.expected_bytes), 0)).where(
            FileTransfer.status.in_(QUOTA_STATUSES),
            or_(
                FileTransfer.expires_at_ms.is_(None),
                FileTransfer.expires_at_ms > now,
            ),
        )
    )
    if int(active_total or 0) + size > TOTAL_QUOTA_BYTES:
        raise AppError.bad_request("服务器取回暂存空间已满，请先清理过期的取回文件")
    free = shutil.disk_usage(settings.transfers_dir).free
    if free - size < DISK_RESERVE_BYTES:
        raise AppError.internal("服务器暂存磁盘空间不足，请稍后重试")

    filename = _basename(remote_path)
    row = FileTransfer(
        device_uid=device.uid,
        device_name=device.display_name,
        remote_path=remote_path,
        filename=filename,
        expected_bytes=size,
        received_bytes=0,
        status=STATUS_PENDING,
        storage_name=_storage_name(filename),
        actor="console",
        created_at_ms=now,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await audit.record(
        session,
        action=audit.ACTION_FILE_TRANSFER_START,
        device=device,
        ip=client_ip(request),
        detail={"transfer_id": row.id, "path": remote_path, "size": size},
    )

    app = request.app
    task = asyncio.create_task(run_transfer(app, row.id), name=f"file-transfer-{row.id}")
    _tasks(app)[row.id] = task
    task.add_done_callback(lambda _t, tid=row.id: _tasks(app).pop(tid, None))
    return stamp(transfer_out(row))


@router.get("/file-transfers")
async def list_transfers(
    limit: int = Query(default=100, ge=1, le=500),
    status: str = Query(default=""),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    statement = select(FileTransfer).order_by(FileTransfer.id.desc()).limit(limit)
    if status:
        statement = statement.where(FileTransfer.status == status)
    rows = (await session.execute(statement)).scalars().all()
    return stamp({"items": [transfer_out(row) for row in rows]})


async def _load_transfer(session: AsyncSession, transfer_id: int) -> FileTransfer:
    row = await session.get(FileTransfer, transfer_id)
    if row is None:
        raise AppError.gone("取回任务不存在")
    return row


@router.get("/file-transfers/{transfer_id}/content")
async def transfer_content(
    transfer_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    row = await _load_transfer(session, transfer_id)
    if row.status != STATUS_READY:
        raise AppError.bad_request("文件尚未取回完成")
    if row.expires_at_ms is not None and now_ms() >= int(row.expires_at_ms):
        raise AppError.gone("暂存已过期，请重新取回")
    settings = request.app.state.settings
    path = _final_path(settings, row.storage_name)
    if not path.is_file():
        raise AppError.gone("暂存文件已丢失，请重新取回")
    await audit.record(
        session,
        action=audit.ACTION_FILE_TRANSFER_RETRIEVE,
        actor="console",
        device_uid=row.device_uid,
        device_name=row.device_name,
        ip=client_ip(request),
        detail={"transfer_id": transfer_id, "path": row.remote_path, "bytes": row.received_bytes},
    )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=_download_name(row),
        headers={"Cache-Control": "no-store", "X-Sha256": row.sha256 or ""},
    )


@router.delete("/file-transfers/{transfer_id}")
async def delete_transfer(
    transfer_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await _load_transfer(session, transfer_id)
    settings = request.app.state.settings
    storage_name = row.storage_name
    snapshot = {
        "transfer_id": transfer_id,
        "path": row.remote_path,
        "status": row.status,
        "device_uid": row.device_uid,
        "name": row.filename,
    }

    app = request.app
    task = _tasks(app).pop(transfer_id, None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            # Bounded: a task stuck in an agent await must not hang the request.
            await asyncio.wait_for(task, timeout=5)

    # Memory rule: commit the row deletion first, unlink the bytes second.
    await session.delete(row)
    await session.commit()
    _unlink(_final_path(settings, storage_name))
    _unlink(_part_path(settings, storage_name))
    await audit.record(
        session,
        action=audit.ACTION_FILE_TRANSFER_DELETE,
        actor="console",
        device_uid=str(snapshot.get("device_uid") or ""),
        device_name="",
        ip=client_ip(request),
        detail=snapshot,
    )
    return stamp({"ok": True, "id": transfer_id})


# -- lifecycle --------------------------------------------------------------


async def expire_transfers(app: Any) -> int:
    """Housekeeping step: drop expired rows, then their bytes, then orphans."""

    database = getattr(app.state, "db", None)
    if database is None:
        return 0
    settings = app.state.settings
    now = now_ms()
    removed: list[str] = []
    async with database.sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(FileTransfer)
                    .where(
                        or_(
                            and_(
                                FileTransfer.expires_at_ms.is_not(None),
                                FileTransfer.expires_at_ms <= now,
                            ),
                            and_(
                                FileTransfer.status.in_((STATUS_FAILED, STATUS_CANCELLED)),
                                FileTransfer.created_at_ms <= now - RETENTION_MS,
                            ),
                            and_(
                                FileTransfer.status == STATUS_READY,
                                FileTransfer.expires_at_ms.is_(None),
                                FileTransfer.created_at_ms <= now - RETENTION_MS,
                            ),
                        )
                    )
                    .order_by(FileTransfer.id)
                    .limit(EXPIRE_BATCH)
                )
            )
            .scalars()
            .all()
        )
        removed = [row.storage_name for row in rows if row.storage_name]
        for row in rows:
            await session.delete(row)
        await session.commit()

    for storage_name in removed:
        _unlink(_final_path(settings, storage_name))
        _unlink(_part_path(settings, storage_name))

    await _sweep_orphans(database, settings, now)
    return len(rows)


async def _sweep_orphans(database: Any, settings: Any, now: int) -> None:
    """Delete spool files no row claims and that are older than the retention.

    Only ``transfers_dir`` is touched.  Age is the second fence: a file being
    written by a just-started task has a fresh mtime and is never a candidate.
    """

    directory: Path = settings.transfers_dir
    try:
        candidates = [
            entry
            for entry in directory.iterdir()
            if entry.is_file() and (now - int(entry.stat().st_mtime * 1000)) > RETENTION_MS
        ]
    except (FileNotFoundError, PermissionError, OSError):
        return
    if not candidates:
        return
    try:
        async with database.sessionmaker() as session:
            owned = {
                str(name)
                for name in (await session.execute(select(FileTransfer.storage_name))).scalars()
                if name
            }
    except Exception as exc:  # noqa: BLE001 - sweep is opportunistic
        log.warning("file transfer owner lookup failed: %s", exc)
        return
    for entry in candidates:
        name = entry.name
        storage = name[:-5] if name.endswith(".part") else name
        if storage and storage not in owned:
            _unlink(entry)


async def recover_interrupted_transfers(app: Any) -> int:
    """Startup: tasks died with the process, so pending/running rows are failed."""

    database = getattr(app.state, "db", None)
    if database is None:
        return 0
    settings = app.state.settings
    now = now_ms()
    names: list[str] = []
    async with database.sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(FileTransfer).where(FileTransfer.status.in_(ACTIVE_STATUSES))
                )
            )
            .scalars()
            .all()
        )
        names = [row.storage_name for row in rows]
        for row in rows:
            row.status = STATUS_FAILED
            row.error = "服务重启，取回中断，请重新发起"
            row.completed_at_ms = now
            row.expires_at_ms = None
        await session.commit()
    for storage_name in names:
        _unlink(_part_path(settings, storage_name))
    return len(names)


async def cancel_all_transfers(app: Any) -> None:
    """Shutdown: cancel every running task before the DB pool closes."""

    tasks = _tasks(app)
    pending = list(tasks.values())
    tasks.clear()
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


__all__ = [
    "RETENTION_MS",
    "cancel_all_transfers",
    "expire_transfers",
    "recover_interrupted_transfers",
    "router",
    "run_transfer",
]
