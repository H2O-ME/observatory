"""Files manager proxy (12 operations + 1 MiB chunked read/write).

The console never speaks to an agent directly: every operation is one journal
command awaited through ``session.request`` with the §5.4 budget (A8).  Device
timestamps are renamed to ``device_time_ms`` by ``dto.file_entry_out`` (A12) and
the top-level ``server_time_ms`` always comes from the server clock.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import now_ms
from ..core.db import get_session
from ..core.errors import AppError
from . import audit
from .deps import client_ip, get_links, require_admin, store
from .devices import device_state_for, load_device
from .dto import budget_ms, file_entry_out, stamp
from .envelope import ConsoleRoute

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/devices",
    tags=["files"],
    dependencies=[Depends(require_admin)],
    route_class=ConsoleRoute,
)

MAX_CHUNK_BYTES = 1024 * 1024
MUTATING_OPS = frozenset({"mkdir", "delete", "rename", "copy", "move", "write"})
ALLOWED_OPS = frozenset(
    {"list", "stat", "mkdir", "delete", "rename", "copy", "move", "search", "drives", "read", "write", "hash"}
)


def _result_to_error(result: Any) -> AppError:
    code = str(getattr(result, "error_code", "") or "")
    message = str(getattr(result, "error", "") or "受控端操作失败")
    if code == "GONE":
        return AppError.gone(message)
    if code == "UNSUPPORTED":
        return AppError.unsupported(message)
    if code == "TIMEOUT" or getattr(result, "status", "") == "timeout":
        return AppError.timeout(message)
    if code == "RATE":
        return AppError.rate(message)
    if code == "AUTH":
        return AppError.auth(message)
    return AppError.bad_request(message)


async def ensure_reachable(request: Request, session: AsyncSession, device: Any) -> None:
    """A13: availability is whatever ``presence.snapshot`` says, never inferred."""

    _effective, snapshot = await device_state_for(session, device)
    capabilities = snapshot.get("capabilities") or {}
    if not capabilities.get("files"):
        raise AppError.unsupported(str(snapshot.get("reason") or "设备当前不可操作"))


async def run_file_op(
    request: Request,
    session: AsyncSession,
    device: Any,
    body: dict[str, Any],
) -> dict[str, Any]:
    await ensure_reachable(request, session, device)
    deadline = budget_ms("files")
    agent_session = get_links(request).get(device.id)
    if agent_session is None:
        raise AppError.unsupported("设备当前没有活动链路")
    result = await agent_session.request("files", body, timeout_s=deadline / 1000)
    if not getattr(result, "ok", False):
        if getattr(result, "status", "") == "cancelled":
            raise AppError.internal(str(getattr(result, "error", "") or "命令已取消"))
        raise _result_to_error(result)
    data = getattr(result, "result", None)
    return {
        "ok": True,
        "op": str(body.get("op") or ""),
        "data": data if isinstance(data, dict) else {},
        "error": None,
        "elapsed_ms": int(getattr(result, "latency_ms", 0) or 0),
        "deadline_ms": deadline,
    }


def _entries(raw: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        entries.append(file_entry_out(item))
    return entries


def _list_payload(body: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    path = str(data.get("path") or body.get("path") or "")
    return stamp(
        {
            "path": path,
            "parent": data.get("parent"),
            "entries": _entries(data.get("entries")),
            "drives": _entries(data.get("drives")),
            "truncated": bool(data.get("truncated")),
        },
        deadline=budget_ms("files"),
    )


@router.get("/{uid}/files")
async def list_files(
    uid: str,
    request: Request,
    path: str = Query(default=""),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await load_device(session, uid)
    body = {"op": "drives"} if not path else {"op": "list", "path": path}
    result = await run_file_op(request, session, device, body)
    return _list_payload(body, result["data"])


@router.post("/{uid}/files/op")
async def file_op(
    uid: str,
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await load_device(session, uid)
    op = str(payload.get("op") or "")
    if op in {"read", "write"}:
        raise AppError.bad_request("读写请使用 /files/raw 分片接口")
    if op not in ALLOWED_OPS:
        raise AppError.bad_request(f"不允许的文件操作: {op}")
    result = await run_file_op(request, session, device, payload)
    if op in MUTATING_OPS:
        await audit.record(
            session,
            action=audit.ACTION_FILE_OP,
            device=device,
            ip=client_ip(request),
            detail={
                "op": op,
                "path": payload.get("path"),
                "target": payload.get("target"),
                "ok": result["ok"],
            },
        )
    return stamp(
        {
            "ok": result["ok"],
            "op": op,
            "data": result["data"],
            "error": result["error"],
            "elapsed_ms": result["elapsed_ms"],
        },
        deadline=result["deadline_ms"],
    )


@router.get("/{uid}/files/raw")
async def read_raw(
    uid: str,
    request: Request,
    path: str,
    offset: int = Query(default=0, ge=0),
    length: int = Query(default=MAX_CHUNK_BYTES, ge=1, le=MAX_CHUNK_BYTES),
    session: AsyncSession = Depends(get_session),
) -> Response:
    device = await load_device(session, uid)
    result = await run_file_op(
        request,
        session,
        device,
        {"op": "read", "path": path, "offset": offset, "length": length},
    )
    data = result["data"]
    try:
        payload = base64.b64decode(str(data.get("data") or ""))
    except (ValueError, TypeError) as exc:
        raise AppError.internal("受控端返回的数据无法解码") from exc
    media = _media_type(path)
    headers = {
        "X-File-Size": str(int(data.get("size") or 0)),
        "X-Chunk-Offset": str(int(data.get("offset") or offset)),
        "X-Chunk-Length": str(len(payload)),
        "X-Eof": "1" if data.get("eof") else "0",
        "X-Server-Time-Ms": str(now_ms()),
        "X-Deadline-Ms": str(budget_ms("files")),
        "Cache-Control": "no-store",
    }
    return Response(content=payload, media_type=media, headers=headers)


@router.put("/{uid}/files/raw")
async def write_raw(
    uid: str,
    request: Request,
    path: str,
    offset: int = Query(default=0, ge=0),
    truncate: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    device = await load_device(session, uid)
    body = await request.body()
    if not body:
        raise AppError.bad_request("空数据")
    if len(body) > MAX_CHUNK_BYTES:
        raise AppError.bad_request("分片超过 1MiB")
    effective = await store().effective(session, device.settings or {})
    max_bytes = int(effective.get("file_upload_max_mb", 512)) * 1024 * 1024
    if offset + len(body) > max_bytes:
        raise AppError.bad_request("文件超过该设备允许的上传上限")

    result = await run_file_op(
        request,
        session,
        device,
        {
            "op": "write",
            "path": path,
            "offset": offset,
            "truncate": truncate,
            "data": base64.b64encode(body).decode("ascii"),
        },
    )
    await audit.record(
        session,
        action=audit.ACTION_FILE_OP,
        device=device,
        ip=client_ip(request),
        detail={"op": "write", "path": path, "offset": offset, "bytes": len(body), "ok": result["ok"]},
    )
    return stamp(
        {
            "ok": result["ok"],
            "op": "write",
            "data": result["data"],
            "error": result["error"],
            "elapsed_ms": result["elapsed_ms"],
        },
        deadline=result["deadline_ms"],
    )


_MEDIA_TYPES = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _media_type(path: str) -> str:
    return _MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


__all__ = ["MAX_CHUNK_BYTES", "ensure_reachable", "router", "run_file_op"]