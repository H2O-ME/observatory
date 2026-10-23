"""0.2.10 数据库 schema 快照（N3 迁移证据）。

WHY：0.2.10 代码删除后，`.localdata/observatory.db` 只是本机运行产物，可能被清理；
而 N3 要求「六张表 + 历史画面平滑迁移」。本文件把 0.2.10 的**真实 DDL** 冻结下来，
供 `tests/contract/test_legacy_db_schema.py` 与 W3 的迁移测试构造一个可信旧库。

采集方式（只读）：
    sqlite3.connect("file:.localdata/observatory.db?mode=ro&immutable=1", uri=True)
    来源文件：`.localdata/observatory.db`（94,208 B，mtime 2026-10-22 00:09）
    采集时表行数：audit=1, commands=0, devices=0, frames=0, packages=0, settings=0
    → **schema 可用，数据近乎为空**；N3 需要「有历史画面」的样本时必须自造数据
      （或使用现场备份）。

⚠️ 只读证据：不要为了新 schema 修改这里。0.3.0 的迁移函数见 server/observatory/core/db.py（W1）。
"""

from __future__ import annotations

LEGACY_DB_FILE = ".localdata/observatory.db"
LEGACY_DB_SIZE_BYTES = 94_208
LEGACY_DB_MTIME = "2026-10-22T00:09:19"
LEGACY_DB_TABLES = ("audit", "commands", "devices", "frames", "packages", "settings")

#: 采集时的行数（用于判断"真实库"是否有可用历史数据）
LEGACY_DB_ROW_COUNTS = {
    "audit": 1,
    "commands": 0,
    "devices": 0,
    "frames": 0,
    "packages": 0,
    "settings": 0,
}

#: 0.3.0 需要在旧表上新增的列（spec §6）
EXPECTED_NEW_COLUMNS = {
    "commands": ("attempts", "error_code"),
    "devices": ("boot_id", "protocol"),
    "frames": ("frame_id",),
}

#: 0.3.0 新增的表（spec §6）
EXPECTED_NEW_TABLES = ("schema_version", "frame_archives", "install_tokens")

DDL = (
    """CREATE TABLE audit (
\tid INTEGER NOT NULL,
\tts_ms BIGINT NOT NULL,
\tactor VARCHAR(64) NOT NULL,
\taction VARCHAR(64) NOT NULL,
\tdevice_uid VARCHAR(64) NOT NULL,
\tdevice_name VARCHAR(128) NOT NULL,
\tip VARCHAR(64) NOT NULL,
\tdetail JSON NOT NULL,
\tPRIMARY KEY (id)
)\n""",
    """CREATE TABLE commands (
\tid VARCHAR(36) NOT NULL,
\tdevice_id INTEGER NOT NULL,
\ttype VARCHAR(48) NOT NULL,
\tpayload JSON NOT NULL,
\tstatus VARCHAR(16) NOT NULL,
\tresult JSON,
\terror TEXT,
\tactor VARCHAR(64) NOT NULL,
\tcreated_at_ms BIGINT NOT NULL,
\tdispatched_at_ms BIGINT,
\tfinished_at_ms BIGINT,
\texpires_at_ms BIGINT NOT NULL,
\tPRIMARY KEY (id),
\tFOREIGN KEY(device_id) REFERENCES devices (id) ON DELETE CASCADE
)\n""",
    """CREATE TABLE devices (
\tid INTEGER NOT NULL,
\tuid VARCHAR(64) NOT NULL,
\tname VARCHAR(128) NOT NULL,
\thostname VARCHAR(255) NOT NULL,
\tos_name VARCHAR(64) NOT NULL,
\tos_version VARCHAR(128) NOT NULL,
\tarch VARCHAR(32) NOT NULL,
\tagent_version VARCHAR(32) NOT NULL,
\ttoken_hash VARCHAR(128) NOT NULL,
\tenrolled_at_ms BIGINT NOT NULL,
\tlast_seen_ms BIGINT NOT NULL,
\tuninstalled_at_ms BIGINT,
\tlast_result_at_ms BIGINT,
\tsettings JSON NOT NULL,
\tsysinfo JSON NOT NULL,
\tnote VARCHAR(255) NOT NULL,
\tPRIMARY KEY (id)
)\n""",
    """CREATE TABLE frames (
\tid INTEGER NOT NULL,
\tdevice_id INTEGER NOT NULL,
\tts_ms BIGINT NOT NULL,
\tsession INTEGER NOT NULL,
\tkind VARCHAR(16) NOT NULL,
\tpath VARCHAR(512) NOT NULL,
\tthumb_path VARCHAR(512),
\twidth INTEGER NOT NULL,
\theight INTEGER NOT NULL,
\tsize_bytes INTEGER NOT NULL,
\tsha256 VARCHAR(64) NOT NULL,
\tPRIMARY KEY (id),
\tFOREIGN KEY(device_id) REFERENCES devices (id) ON DELETE CASCADE
)\n""",
    """CREATE TABLE packages (
\tid INTEGER NOT NULL,
\tkind VARCHAR(16) NOT NULL,
\tversion VARCHAR(32) NOT NULL,
\tfilename VARCHAR(255) NOT NULL,
\tsize_bytes INTEGER NOT NULL,
\tsha256 VARCHAR(64) NOT NULL,
\tnotes VARCHAR(255) NOT NULL,
\tcreated_at_ms BIGINT NOT NULL,
\tPRIMARY KEY (id),
\tCONSTRAINT uq_packages_kind_version UNIQUE (kind, version)
)\n""",
    """CREATE TABLE settings (
\t"key" VARCHAR(64) NOT NULL,
\tvalue JSON NOT NULL,
\tupdated_at_ms BIGINT NOT NULL,
\tPRIMARY KEY ("key")
)\n""",
    "CREATE INDEX ix_audit_action ON audit (action)",
    "CREATE INDEX ix_audit_device_uid ON audit (device_uid)",
    "CREATE INDEX ix_audit_ts_ms ON audit (ts_ms)",
    "CREATE INDEX ix_commands_device_id ON commands (device_id)",
    "CREATE INDEX ix_commands_device_status ON commands (device_id, status)",
    "CREATE INDEX ix_commands_status ON commands (status)",
    "CREATE INDEX ix_devices_last_seen_ms ON devices (last_seen_ms)",
    "CREATE UNIQUE INDEX ix_devices_uid ON devices (uid)",
    "CREATE INDEX ix_frames_device_id ON frames (device_id)",
    "CREATE INDEX ix_frames_device_session ON frames (device_id, session)",
    "CREATE INDEX ix_frames_device_ts ON frames (device_id, ts_ms)",
    "CREATE INDEX ix_frames_ts_ms ON frames (ts_ms)",
    "CREATE INDEX ix_packages_version ON packages (version)",
)

#: 0.2.10 的 JSON 列（SQLite 存 TEXT，但 ORM 以 JSON 读写；迁移时必须保持兼容）
JSON_COLUMNS = {
    "audit": ("detail",),
    "commands": ("payload", "result"),
    "devices": ("settings", "sysinfo"),
    "settings": ("value",),
}


def create_legacy_schema(connection) -> None:
    """在给定 sqlite3.Connection 上建出 0.2.10 schema（不含数据）。

    N3 用它 + 合成数据构造"有历史画面"的旧库；也可直接对真实旧库做只读校验。
    """

    for statement in DDL:
        connection.execute(statement)
    connection.commit()


__all__ = [
    "DDL",
    "EXPECTED_NEW_COLUMNS",
    "EXPECTED_NEW_TABLES",
    "JSON_COLUMNS",
    "LEGACY_DB_FILE",
    "LEGACY_DB_MTIME",
    "LEGACY_DB_ROW_COUNTS",
    "LEGACY_DB_SIZE_BYTES",
    "LEGACY_DB_TABLES",
    "create_legacy_schema",
]