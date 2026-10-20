"""Full file management proxy.

Every operation is an audited command on the device, so the console can do what
Explorer can do — list drives, browse, create, rename, copy, move, delete, search,
read and write — with the real NTFS error surfaced instead of a generic failure.

Reads and writes are chunked. The browser reads 1 MiB at a time and assembles a
``Blob`` for download; uploads resume from any offset, so a multi-gigabyte file
over a slow link does not restart from zero. Chunking also keeps each command well
inside its timeout, which is what made the old single-shot implementation die on
large folders.
"""

from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import client_ip, get_session, require_admin
from ..models import Device
from ..schemas import FileEntryOut, FileListOut, FileOpRequest, FileOpResult
from ..services import audit, commands as command_service, views
from ..services.runtime import Runtime, get_runtime

log = logging.getLogger(__name__)
router = APIRouter(prefix="/devices", tags=["files"], dependencies=[Depends(require_admin)])

MAX_CHUNK_BYTES = 1024 * 1024
MUTATING_OPS = frozenset({"mkdir", "delete", "rename", "copy", "move", "write"})


async def load_device(session: AsyncSession, uid: str) -> Device:
    from ..api.devices import load_device as _load

    return await _load(session, uid)


async def _ensure_reachable(runtime: Runtime, session: AsyncSession, device: Device) -> None:
    payload = await views.device_out(runtime, session, device)
    capabilities = payload["presence"]["capabilities"]
    if not capabilities.get("files"):
        raise HTTPException(status_code=409, detail=payload["presence"]["reason"] or "设备当前不可操作")


async def run_file_op(
    runtime: Runtime,
    session: AsyncSession,
    device: Device,
    payload: dict,
    *,
    timeout_s: float = 20.0,
) -> dict:
    await _ensure_reachable(runtime, session, device)
    command = await command_service.create(
        session,
        device=device,
        command_type="files",
        payload=payload,
        actor="console",
        bus=runtime.bus,
        agent_hub=runtime.agent_hub,
        console_hub=runtime.console_hub,
    )
    return await command_service.wait_for(
        session,
        command,
        bus=runtime.bus,
        timeout_s=timeout_s,
        device_uid=device.uid,
        console_hub=runtime.console_hub,
    )


def _entries(raw: list | None) -> list[dict]:
    entries: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        entries.append(
            {
                "name": str(item.get("name", "")),
                "path": str(item.get("path", "")),
                "is_dir": bool(item.get("is_dir")),
                "size": int(item.get("size") or 0),
                "mtime_ms": int(item.get("mtime_ms") or 0),
                "hidden": bool(item.get("hidden")),
                "drive": str(item.get("drive", "")),
            }
        )
    return entries


@router.get("/{uid}/files", response_model=FileListOut)
async def list_files(
    uid: str,
    path: str = Query(default="", description="目录路径，留空表示列出驱动器"),
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> FileListOut:
    device = await load_device(session, uid)
    if not path:
        result = await run_file_op(runtime, session, device, {"op": "drives"}, timeout_s=15)
        if not result.get("ok"):
            raise HTTPException(status_code=502, detail=result.get("error") or "无法列出驱动器")
        data = result.get("data") or {}
        return FileListOut(
            path="",
            parent=None,
            entries=[],
            drives=[FileEntryOut(**_entry) for _entry in _entries(data.get("drives"))],
            truncated=False,
            server_time_ms=data.get("server_time_ms", 0) or 0,
        )

    result = await run_file_op(runtime, session, device, {"op": "list", "path": path}, timeout_s=25)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error") or "读取目录失败")
    data = result.get("data") or {}
    return FileListOut(
        path=str(data.get("path") or path),
        parent=data.get("parent"),
        entries=[FileEntryOut(**_entry) for _entry in _entries(data.get("entries"))],
        drives=[FileEntryOut(**_entry) for _entry in _entries(data.get("drives"))],
        truncated=bool(data.get("truncated")),
        server_time_ms=int(data.get("server_time_ms") or 0),
    )


@router.post("/{uid}/files/op", response_model=FileOpResult)
async def file_op(
    uid: str,
    payload: FileOpRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> FileOpResult:
    device = await load_device(session, uid)
    body = payload.model_dump(exclude_none=True)

    if payload.op in {"read", "write"}:
        raise HTTPException(status_code=400, detail="读写请使用 /files/raw 分片接口")

    result = await run_file_op(runtime, session, device, body, timeout_s=60)

    if payload.op in MUTATING_OPS:
        await audit.record(
            session,
            action=audit.ACTION_FILE_OP,
            device=device,
            ip=client_ip(request),
            detail={"op": payload.op, "path": payload.path, "target": payload.target, "ok": result.get("ok")},
        )
    return FileOpResult(
        ok=bool(result.get("ok")),
        op=str(result.get("op") or payload.op),
        data=result.get("data") or {},
        error=result.get("error"),
        elapsed_ms=int(result.get("elapsed_ms") or 0),
    )


@router.get("/{uid}/files/raw")
async def read_raw(
    uid: str,
    path: str,
    offset: int = Query(default=0, ge=0),
    length: int = Query(default=MAX_CHUNK_BYTES, ge=1, le=MAX_CHUNK_BYTES),
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> Response:
    device = await load_device(session, uid)
    result = await run_file_op(
        runtime, session, device, {"op": "read", "path": path, "offset": offset, "length": length}, timeout_s=45
    )
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error") or "读取文件失败")
    data = result.get("data") or {}
    try:
        payload = base64.b64decode(data.get("data") or "")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="受控端返回的数据无法解码") from exc

    from ..services.storage import MEDIA_TYPES
    from pathlib import Path

    media = MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")
    headers = {
        "X-File-Size": str(data.get("size") or 0),
        "X-Chunk-Offset": str(data.get("offset") or offset),
        "X-Chunk-Length": str(len(payload)),
        "X-Eof": "1" if data.get("eof") else "0",
        "Cache-Control": "no-store",
    }
    return Response(content=payload, media_type=media, headers=headers)


@router.put("/{uid}/files/raw", response_model=FileOpResult)
async def write_raw(
    uid: str,
    request: Request,
    path: str,
    offset: int = Query(default=0, ge=0),
    truncate: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> FileOpResult:
    device = await load_device(session, uid)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="空数据")
    if len(body) > MAX_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail="分片超过 1MiB")

    result = await run_file_op(
        runtime,
        session,
        device,
        {
            "op": "write",
            "path": path,
            "offset": offset,
            "truncate": truncate,
            "data": base64.b64encode(body).decode("ascii"),
        },
        timeout_s=45,
    )
    await audit.record(
        session,
        action=audit.ACTION_FILE_OP,
        device=device,
        ip=client_ip(request),
        detail={"op": "write", "path": path, "offset": offset, "bytes": len(body), "ok": result.get("ok")},
    )
    return FileOpResult(
        ok=bool(result.get("ok")),
        op="write",
        data=result.get("data") or {},
        error=result.get("error"),
        elapsed_ms=int(result.get("elapsed_ms") or 0),
    )


__all__ = ["MAX_CHUNK_BYTES", "router", "run_file_op"]