"""Agent package management — the input side of auto-update."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import client_ip, get_session, require_admin
from ..models import Device, now_ms
from ..schemas import OkOut, PackageOut, UpdateStatusOut
from ..services import audit, events
from ..services.packages import PackageError
from ..services.runtime import Runtime, get_runtime
from ..version import version_tuple

log = logging.getLogger(__name__)
router = APIRouter(tags=["updates"], dependencies=[Depends(require_admin)])


def _package_out(runtime: Runtime, package) -> PackageOut:
    return PackageOut(
        version=package.version,
        filename=package.filename,
        size_bytes=package.size_bytes,
        sha256=package.sha256,
        notes=package.notes,
        created_at_ms=package.created_at_ms,
        download_url=runtime.packages.url_for(package),
    )


@router.get("/updates", response_model=UpdateStatusOut)
async def update_status(
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> UpdateStatusOut:
    packages = await runtime.packages.list(session, "agent") if runtime.packages else []
    latest = packages[0].version if packages else None
    devices = list((await session.execute(select(Device))).scalars().all())
    outdated: list[dict[str, str]] = []
    if latest:
        latest_tuple = version_tuple(latest)
        for device in devices:
            try:
                if version_tuple(device.agent_version or "0.0.0") < latest_tuple:
                    outdated.append(
                        {"uid": device.uid, "name": device.display_name, "version": device.agent_version or "未知"}
                    )
            except ValueError:
                continue
    return UpdateStatusOut(
        server_version=runtime.version,
        latest_agent_version=latest,
        outdated=outdated,
        packages=[_package_out(runtime, package) for package in packages],
        server_time_ms=now_ms(),
    )


@router.post("/updates/agent", response_model=PackageOut)
async def upload_package(
    request: Request,
    file: UploadFile = File(...),
    notes: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> PackageOut:
    if runtime.packages is None:
        raise HTTPException(status_code=503, detail="安装包服务未就绪")
    data = await file.read()
    try:
        package = await runtime.packages.save(
            session, filename=file.filename or "observatory-agent.zip", data=data, notes=notes
        )
    except PackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await audit.record(
        session,
        action=audit.ACTION_PACKAGE_UPLOAD,
        ip=client_ip(request),
        detail={"version": package.version, "bytes": package.size_bytes},
    )
    await events.emit(
        runtime.console_hub,
        events.EVENT_VERSION,
        data={"agent_version": package.version, "action": "package_uploaded"},
    )
    log.info("agent package %s uploaded (%d bytes)", package.version, package.size_bytes)
    return _package_out(runtime, package)


@router.delete("/updates/agent/{version}", response_model=OkOut)
async def delete_package(
    version: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> OkOut:
    if runtime.packages is None:
        raise HTTPException(status_code=503, detail="安装包服务未就绪")
    deleted = await runtime.packages.delete(session, version, "agent")
    if not deleted:
        raise HTTPException(status_code=404, detail="没有这个版本")
    await audit.record(
        session, action=audit.ACTION_PACKAGE_DELETE, ip=client_ip(request), detail={"version": version}
    )
    return OkOut(message=f"已删除 {version}")


__all__ = ["router"]