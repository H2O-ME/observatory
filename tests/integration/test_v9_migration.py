"""P4(v9): 票据/墓碑归档进 audit 后删表。

这条迁移是"删除数据结构"里风险最高的一类：删错了就永久丢运维证据。所以断言
两件事——(1) 每张票据/每条墓碑在 audit 里各留一行；(2) 三张表确实不存在了。
用一份合成的 v8 库直接跑迁移，不经过 app。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_V8_SCHEMA = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at_ms BIGINT NOT NULL);
INSERT INTO schema_version (version, applied_at_ms) VALUES (8, 1700000000000);

CREATE TABLE audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms BIGINT NOT NULL,
    actor VARCHAR(64) NOT NULL,
    action VARCHAR(64) NOT NULL,
    device_uid VARCHAR(64) NOT NULL,
    device_name VARCHAR(128) NOT NULL,
    ip VARCHAR(64) NOT NULL,
    detail JSON NOT NULL
);
CREATE INDEX ix_audit_action ON audit (action);

CREATE TABLE devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid VARCHAR(64) NOT NULL,
    token_hash VARCHAR(128) NOT NULL,
    uninstalled_at_ms BIGINT,
    purge_pending_at_ms BIGINT
);

CREATE TABLE install_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash VARCHAR(128) NOT NULL,
    created_by VARCHAR(64) NOT NULL DEFAULT 'console',
    created_at_ms BIGINT NOT NULL,
    expires_at_ms BIGINT NOT NULL DEFAULT 0,
    used_at_ms BIGINT,
    used_device_id INTEGER,
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    last_used_at_ms BIGINT,
    revoked_at_ms BIGINT
);
CREATE UNIQUE INDEX ux_install_tokens_hash ON install_tokens (token_hash);

CREATE TABLE install_token_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id INTEGER NOT NULL,
    device_id INTEGER NOT NULL,
    used_at_ms BIGINT NOT NULL,
    ip VARCHAR(64) NOT NULL DEFAULT '',
    hostname VARCHAR(128) NOT NULL DEFAULT ''
);

CREATE TABLE deleted_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid VARCHAR(64) NOT NULL,
    token_hash VARCHAR(128) NOT NULL,
    deleted_at_ms BIGINT NOT NULL
);
CREATE UNIQUE INDEX ux_deleted_devices_token ON deleted_devices (token_hash);

INSERT INTO install_tokens (id, token_hash, created_by, created_at_ms, expires_at_ms, max_uses, used_count)
VALUES (7, 'hash-ticket-7', 'console', 1700000000000, 0, 0, 2);
INSERT INTO install_token_uses (token_id, device_id, used_at_ms, ip, hostname)
VALUES (7, 1, 1700000000001, '10.0.0.9', 'pc-1');
INSERT INTO deleted_devices (uid, token_hash, deleted_at_ms)
VALUES ('OBS-GONE-1', 'hash-gone-1', 1700000000002);
"""


def _migrate(tmp_dir: Path) -> sqlite3.Cursor:  # pragma: no cover - helper
    raise AssertionError("unused")


def test_v8_to_v9_archives_and_drops_legacy_tables(tmp_dir: Path) -> None:
    from observatory.core.config import Settings
    from observatory.core.db import SCHEMA_VERSION, Database

    database_path = tmp_dir / "observatory.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(_V8_SCHEMA)
        connection.commit()
    finally:
        connection.close()

    settings = Settings(
        data_dir=tmp_dir,
        secret_key="test-secret-key-0123456789abcdef0123456789",
        admin_password="test-admin-password",
    )
    database = Database(settings)

    async def run():
        report = await database.migrate()
        await database.engine.dispose()
        return report

    report = asyncio.run(run())
    assert report.from_version == 8
    assert report.to_version == SCHEMA_VERSION
    assert report.applied == (9,)
    assert report.backup_path is not None and report.backup_path.exists()

    connection = sqlite3.connect(database_path)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not {"install_tokens", "install_token_uses", "deleted_devices"} & tables

        rows = connection.execute(
            "SELECT action, device_uid, ip, detail FROM audit WHERE actor = 'migration' ORDER BY id"
        ).fetchall()
        actions = [row[0] for row in rows]
        assert actions == [
            "legacy.ticket_archive",
            "legacy.ticket_binding_archive",
            "legacy.tombstone_archive",
        ], actions

        ticket = json.loads(rows[0][3])
        assert ticket["id"] == 7 and ticket["used_count"] == 2
        binding = json.loads(rows[1][3])
        assert binding["token_id"] == 7 and binding["hostname"] == "pc-1"
        assert rows[1][2] == "10.0.0.9", "绑定记录的来源 IP 必须保留"
        tombstone = json.loads(rows[2][3])
        assert tombstone["deleted_at_ms"] == 1700000000002
        assert rows[2][1] == "OBS-GONE-1", "墓碑归档要把 uid 写进 audit.device_uid"

        # 幂等：再跑一次迁移不应重复归档、也不应报错
        assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9
    finally:
        connection.close()

    async def rerun():
        report = await database.migrate()
        await database.engine.dispose()
        return report

    again = asyncio.run(rerun())
    assert again.applied == ()

    connection = sqlite3.connect(database_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM audit WHERE actor = 'migration'").fetchone()[0]
        assert count == 3, "重复迁移不得重复写归档"
    finally:
        connection.close()
