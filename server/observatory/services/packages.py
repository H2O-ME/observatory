"""Agent package store — the source of truth for auto-update.

A package is a zip produced by the server-side build (see ``deploy/build-agent.sh``)
whose filename encodes its version: ``observatory-agent-0.2.1.zip``. Uploading it
through the console makes it immediately available to every device whose version
is older, so an upgrade is a build + an upload, never a reinstall.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Package, now_ms
from ..version import parse_version, version_tuple

log = logging.getLogger(__name__)

SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")
#: ``observatory-agent-0.2.1.zip`` (product prefix optional, kind required).
FILENAME_RE = re.compile(
    r"^(?:(?P<product>[a-z]+)-)?(?P<kind>[a-z]+)-(?P<version>\d+\.\d+\.\d+)\.zip$"
)
MAX_PACKAGE_BYTES = 256 * 1024 * 1024

#: The oldest agent build that can safely replace itself through auto-update.
#:
#: Do not lower this to "make updates work again". Agents below 0.2.5 apply an
#: update by spawning a helper that only did ``shutil.copy2(sys.executable)``, but
#: every shipped agent is a PyInstaller *onedir* build: the copied exe was missing
#: its sibling ``_internal/`` runtime, so the helper died immediately while the old
#: agent still shut itself down unconditionally — the host lost its service and
#: nothing brought it back. 0.2.5 introduced whole-directory staging (``copytree``)
#: plus ``verify_helper_alive``; only builds at or above it may be told to upgrade.
MIN_SELF_UPDATE_VERSION = "0.2.5"


def can_self_update(current_version: str | None) -> bool:
    """True when *current_version* may apply an automatic update without bricking.

    This is the single version gate: the WebSocket welcome, the WebSocket pong and
    the HTTP heartbeat all call it before handing out an ``update`` object. Unknown
    or malformed versions return ``False`` — keeping an old agent running is always
    safer than an upgrade it cannot finish.
    """

    try:
        return parse_version(current_version or "0.0.0") >= parse_version(MIN_SELF_UPDATE_VERSION)
    except ValueError:
        return False


class PackageError(ValueError):
    pass


def parse_filename(filename: str) -> tuple[str, str]:
    """Derive ``(kind, version)`` from a package filename."""

    name = Path(filename).name
    match = FILENAME_RE.match(name)
    if match:
        return match.group("kind"), match.group("version")
    match = SEMVER_RE.search(name)
    if not match:
        raise PackageError(f"文件名里没有版本号: {name}（示例 observatory-agent-0.2.1.zip）")
    return "agent", match.group(0)


class PackageService:
    def __init__(self, settings) -> None:
        self.settings = settings

    @property
    def root(self) -> Path:
        return Path(self.settings.packages_dir)

    def file_for(self, package: Package) -> Path:
        return self.root / package.filename

    def url_for(self, package: Package) -> str:
        return f"/api/v1/public/agent/{package.version}/package"

    def update_payload(self, package: Package) -> dict[str, Any]:
        """The one shape of the ``update`` object pushed to an agent.

        Welcome, pong and the HTTP heartbeat must agree field-for-field: the agent
        parses this dict by key, so keeping a single builder here is what stops the
        three transports from drifting apart.
        """

        return {
            "version": package.version,
            "url": self.url_for(package),
            "sha256": package.sha256,
            "size_bytes": package.size_bytes,
        }

    async def save(
        self,
        session: AsyncSession,
        *,
        filename: str,
        data: bytes,
        notes: str = "",
    ) -> Package:
        if not data:
            raise PackageError("上传内容为空")
        if len(data) > MAX_PACKAGE_BYTES:
            raise PackageError("安装包超过 256MB 上限")
        if not data.startswith(b"PK"):
            raise PackageError("不是有效的 zip 文件")

        kind, version = parse_filename(filename)
        if kind != "agent":
            raise PackageError(f"暂不支持的包类型: {kind}")

        stored_name = f"observatory-{kind}-{version}.zip"
        target = self.root / stored_name
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".zip.part")
        tmp.write_bytes(data)
        tmp.replace(target)

        digest = hashlib.sha256(data).hexdigest()
        existing = await session.execute(select(Package).where(Package.kind == kind, Package.version == version))
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

        added = 0
        self.root.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.root.glob("*.zip")):
            try:
                kind, version = parse_filename(path.name)
            except PackageError:
                continue
            existing = await session.execute(
                select(Package).where(Package.kind == kind, Package.version == version)
            )
            if existing.scalars().first() is not None:
                continue
            # Path.read_bytes is synchronous; awaiting it raises TypeError and
            # takes the whole API down at start-up the moment a zip is dropped
            # into the packages directory without a database row.
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
        packages.sort(key=lambda item: version_tuple(item.version), reverse=True)
        return packages

    async def latest(self, session: AsyncSession, kind: str = "agent") -> Package | None:
        packages = await self.list(session, kind)
        return packages[0] if packages else None

    async def delete(self, session: AsyncSession, version: str, kind: str = "agent") -> bool:
        result = await session.execute(select(Package).where(Package.kind == kind, Package.version == version))
        package = result.scalars().first()
        if package is None:
            return False
        path = self.file_for(package)
        await session.delete(package)
        await session.commit()
        try:
            path.unlink()
        except OSError:
            pass
        return True


__all__ = [
    "MAX_PACKAGE_BYTES",
    "MIN_SELF_UPDATE_VERSION",
    "PackageError",
    "PackageService",
    "can_self_update",
    "parse_filename",
]