"""Agent packages and the update offer.

Package storage moved from the old domain service into the console because the
only writer is an authenticated console upload.  Two guarantees live here:

* **A3** -- an ``update`` command is rejected server-side unless the payload
  carries a non-empty ``sha256``; the offer built by :func:`update_payload`
  always includes the digest of the exact bytes that were stored;
* **C6** -- uploads/deletes are observable: failures raise or are logged with
  the version, never swallowed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import presence
from ..core.clock import now_ms
from ..core.config import (
    MIN_SELF_UPDATE_VERSION,
    can_self_update,
    is_newer,
    parse_version,
    resolve_version,
)
from ..core.db import get_session
from ..core.errors import AppError
from ..core.models import Device, Package
from . import audit
from .deps import client_ip, get_settings, require_admin
from .dto import stamp
from .envelope import ConsoleRoute

log = logging.getLogger(__name__)

router = APIRouter(tags=["updates"], dependencies=[Depends(require_admin)], route_class=ConsoleRoute)

SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")
FILENAME_RE = re.compile(
    r"^(?:(?P<product>[a-z]+)-)?(?P<kind>[a-z]+)-(?P<version>\d+\.\d+\.\d+)\.zip$"
)
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
DEFAULT_UPDATE_URL = "/api/v1/public/agent/{version}/package"


class PackageError(ValueError):
    pass


def parse_filename(filename: str) -> tuple[str, str]:
    name = Path(filename).name
    match = FILENAME_RE.match(name)
    if match:
        return match.group("kind"), match.group("version")
    fallback = SEMVER_RE.search(name)
    if not fallback:
        raise PackageError(f"文件名里没有版本号: {name}（示例 observatory-agent-0.2.1.zip）")
    return "agent", fallback.group(0)


def require_sha256(payload: dict[str, Any]) -> str:
    """A3: a forced update without a digest is rejected before it reaches a device."""

    digest = str(payload.get("sha256") or "").strip().lower()
    if not digest or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AppError.bad_request("update 命令必须携带 64 位 sha256 摘要")
    payload["sha256"] = digest
    return digest


class PackageService:
    """Disk + database package store."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    @property
    def root(self) -> Path:
        return Path(self.settings.packages_dir)

    def file_for(self, package: Package) -> Path:
        return self.root / package.filename

    def url_for(self, package: Package) -> str:
        return DEFAULT_UPDATE_URL.format(version=package.version)

    def update_payload(self, package: Package) -> dict[str, Any]:
        return {
            "version": package.version,
            "url": self.url_for(package),
            "sha256": package.sha256,
            "size_bytes": package.size_bytes,
        }

    async def save(self, session: AsyncSession, *, filename: str, data: bytes, notes: str = "") -> Package:
        if not data:
            raise PackageError("上传内容为空")
        if len(data) > MAX_PACKAGE_BYTES:
            raise PackageError("安装包超过 256MB 上限")
        if not data.startswith(b"PK"):
            raise PackageError("不是有效的 zip 文件")

        kind, version = parse_filename(filename)
        if kind != "agent":
            raise PackageError(f"暂不支持的包类型: {kind}")
        parse_version(version)

        stored_name = f"observatory-{kind}-{version}.zip"
        target = self.root / stored_name
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".zip.part")
        await asyncio.to_thread(tmp.write_bytes, data)
        await asyncio.to_thread(tmp.replace, target)

        digest = hashlib.sha256(data).hexdigest()
        existing = await session.execute(
            select(Package).where(Package.kind == kind, Package.version == version)
        )
        package = existing.scalars().first()
        if package is None:
            package = Package(kind=kind, version=version, filename=stored_name)
            session.add(package)
        package.filename = stored_name
        package.size_bytes = len(data)
        package.sha256 = digest
        package.notes = notes[:255]
        package.created_at_ms = now_ms()
        await session.commit()
        await session.refresh(package)
        log.info("stored agent package %s (%d bytes)", version, len(data))
        return package

    async def discover(self, session: AsyncSession) -> int:
        """Register zips that exist on disk but not in the database."""

        self.root.mkdir(parents=True, exist_ok=True)
        added = 0
        for path in sorted(self.root.glob("*.zip")):
            try:
                kind, version = parse_filename(path.name)
                parse_version(version)
            except (PackageError, ValueError):
                log.warning("ignoring package file with invalid name: %s", path.name)
                continue
            existing = await session.execute(
                select(Package).where(Package.kind == kind, Package.version == version)
            )
            if existing.scalars().first() is not None:
                continue
            data = await asyncio.to_thread(path.read_bytes)
            session.add(
                Package(
                    kind=kind,
                    version=version,
                    filename=path.name,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    notes="discovered on disk",
                )
            )
            added += 1
        if added:
            await session.commit()
        return added

    async def list(self, session: AsyncSession, kind: str = "agent") -> list[Package]:
        result = await session.execute(select(Package).where(Package.kind == kind))
        packages = list(result.scalars().all())
        packages.sort(key=lambda item: parse_version(item.version), reverse=True)
        return packages

    async def latest(self, session: AsyncSession, kind: str = "agent") -> Package | None:
        packages = await self.list(session, kind)
        return packages[0] if packages else None

    async def delete(self, session: AsyncSession, version: str, kind: str = "agent") -> bool:
        result = await session.execute(
            select(Package).where(Package.kind == kind, Package.version == version)
        )
        package = result.scalars().first()
        if package is None:
            return False
        path = self.file_for(package)
        await session.delete(package)
        await session.commit()
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            log.warning("package file already missing: %s", path)
        except OSError as exc:
            log.warning("could not remove package file %s: %s", path, exc)
        return True

    async def update_offer(self, session: AsyncSession, device: Device) -> dict[str, Any] | None:
        """The offer an agent may receive, gated by the version floor (A3/A13)."""

        if not can_self_update(device.agent_version):
            return None
        package = await self.latest(session, "agent")
        if package is None:
            return None
        if not package.sha256:
            log.error("refusing to offer package %s without sha256", package.version)
            return None
        if not is_newer(package.version, device.agent_version or "0.0.0"):
            return None
        return self.update_payload(package)


def service(request: Request) -> PackageService:
    return PackageService(get_settings(request))


async def latest_package(session: AsyncSession, kind: str = "agent") -> Package | None:
    result = await session.execute(
        select(Package).where(Package.kind == kind).order_by(Package.created_at_ms.desc())
    )
    return result.scalars().first()


async def latest_version(session: AsyncSession) -> str | None:
    package = await latest_package(session)
    return package.version if package else None


@router.get("/updates")
async def update_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    packages = await service(request).list(session, "agent")
    latest = packages[0].version if packages else None
    devices = list((await session.execute(select(Device))).scalars().all())
    outdated: list[dict[str, str]] = []
    if latest:
        latest_tuple = parse_version(latest)
        for device in devices:
            try:
                if parse_version(device.agent_version or "0.0.0") < latest_tuple:
                    outdated.append(
                        {
                            "uid": device.uid,
                            "name": device.display_name,
                            "version": device.agent_version or "未知",
                        }
                    )
            except ValueError:
                # A malformed version is reported, not compared: it needs a
                # reinstall, not a firmware-style sort.
                outdated.append(
                    {"uid": device.uid, "name": device.display_name, "version": device.agent_version or "未知"}
                )
    return stamp(
        {
            "server_version": resolve_version(),
            "latest_agent_version": latest,
            "outdated": outdated,
            "packages": [
                {
                    "version": package.version,
                    "filename": package.filename,
                    "size_bytes": package.size_bytes,
                    "sha256": package.sha256,
                    "notes": package.notes,
                    "created_at_ms": package.created_at_ms,
                    "download_url": service(request).url_for(package),
                }
                for package in packages
            ],
        }
    )


@router.post("/updates/agent")
async def upload_package(
    request: Request,
    file: UploadFile = File(...),
    notes: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    data = await file.read()
    packages = service(request)
    try:
        package = await packages.save(
            session,
            filename=file.filename or "observatory-agent.zip",
            data=data,
            notes=notes,
        )
    except (PackageError, ValueError) as exc:
        raise AppError.bad_request(str(exc)) from exc

    await audit.record(
        session,
        action=audit.ACTION_PACKAGE_UPLOAD,
        ip=client_ip(request),
        detail={"version": package.version, "bytes": package.size_bytes, "sha256": package.sha256},
    )
    presence.set_latest_agent_version(package.version)
    return stamp(
        {
            "version": package.version,
            "filename": package.filename,
            "size_bytes": package.size_bytes,
            "sha256": package.sha256,
            "notes": package.notes,
            "created_at_ms": package.created_at_ms,
            "download_url": packages.url_for(package),
        }
    )


@router.delete("/updates/agent/{version}")
async def delete_package(
    version: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    packages = service(request)
    deleted = await packages.delete(session, version, "agent")
    if not deleted:
        raise AppError.gone(f"没有 {version} 版本的安装包")
    await audit.record(
        session,
        action=audit.ACTION_PACKAGE_DELETE,
        ip=client_ip(request),
        detail={"version": version},
    )
    latest = await packages.latest(session, "agent")
    presence.set_latest_agent_version(latest.version if latest else None)
    return stamp({"ok": True, "message": f"已删除 {version}"})


__all__ = [
    "DEFAULT_UPDATE_URL",
    "MIN_SELF_UPDATE_VERSION",
    "MAX_PACKAGE_BYTES",
    "PackageError",
    "PackageService",
    "can_self_update",
    "latest_package",
    "latest_version",
    "parse_filename",
    "require_sha256",
    "router",
    "service",
]