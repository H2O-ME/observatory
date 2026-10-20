"""Async SQLite engine + a tiny additive migration helper.

SQLite in WAL mode is plenty for a fleet of this size, but it needs the pragmas
set on *every* connection (not once), and schema changes have to be additive
because there is no migration tool in the image. ``_add_missing_columns`` reads
the live table info and issues ``ALTER TABLE ... ADD COLUMN`` for anything the
models declare but the file does not have yet, which keeps an in-place upgrade
from one version to the next working without losing data.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import Settings
from .models import Base

log = logging.getLogger(__name__)

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=8000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-16000",
)


class Database:
    def __init__(self, settings: Settings) -> None:
        settings.ensure_dirs()
        db_path: Path = settings.db_path
        self.url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
        self.engine: AsyncEngine = create_async_engine(
            self.url,
            echo=False,
            pool_pre_ping=True,
            connect_args={"timeout": 15},
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        event.listen(self.engine.sync_engine, "connect", _on_connect)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _add_missing_columns(conn)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()


def _on_connect(dbapi_connection, _record) -> None:  # pragma: no cover - driver hook
    cursor = dbapi_connection.cursor()
    try:
        for pragma in PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


async def _add_missing_columns(conn) -> None:
    """Add columns that exist on the models but not yet in the SQLite file."""

    existing_tables = await _table_names(conn)
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = await _column_names(conn, table.name)
        for column in table.columns:
            if column.name in have:
                continue
            ddl_type = column.type.compile(dialect=conn.dialect)
            default_sql = ""
            if column.default is not None and getattr(column.default, "is_scalar", False):
                value = column.default.arg
                if isinstance(value, bool):
                    default_sql = f" DEFAULT {1 if value else 0}"
                elif isinstance(value, (int, float)):
                    default_sql = f" DEFAULT {value}"
                elif isinstance(value, str):
                    escaped = value.replace("'", "''")
                    default_sql = f" DEFAULT '{escaped}'"
            statement = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}{default_sql}'
            log.info("migrating: %s", statement)
            await conn.execute(text(statement))


async def _table_names(conn) -> set[str]:
    result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    return {row[0] for row in result.fetchall()}


async def _column_names(conn, table: str) -> set[str]:
    result = await conn.execute(text(f'PRAGMA table_info("{table}")'))
    return {row[1] for row in result.fetchall()}


__all__ = ["Database"]