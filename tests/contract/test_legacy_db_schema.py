"""0.2.10 数据库 schema 契约（N3 迁移的证据保护）。"""

from __future__ import annotations

import sqlite3

import pytest

from tests.contract.fixtures import legacy_db_0_2_10 as legacy_db

pytestmark = pytest.mark.contract


def test_schema_has_exactly_the_six_legacy_tables() -> None:
    assert set(legacy_db.LEGACY_DB_TABLES) == {"audit", "commands", "devices", "frames", "packages", "settings"}
    table_ddl = [statement for statement in legacy_db.DDL if statement.startswith("CREATE TABLE")]
    assert len(table_ddl) == 6
    for table in legacy_db.LEGACY_DB_TABLES:
        assert any(f"CREATE TABLE {table} " in statement for statement in table_ddl), table


def test_schema_is_replayable_on_empty_database() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        legacy_db.create_legacy_schema(connection)
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table' order by name")
        }
        assert tables == set(legacy_db.LEGACY_DB_TABLES)
    finally:
        connection.close()


def test_0_3_0_expected_columns_and_tables_are_documented() -> None:
    """spec §6 的新增列/表必须被迁移测试覆盖（W3）。"""

    assert set(legacy_db.EXPECTED_NEW_COLUMNS) == {"commands", "devices", "frames"}
    assert "attempts" in legacy_db.EXPECTED_NEW_COLUMNS["commands"]
    assert "error_code" in legacy_db.EXPECTED_NEW_COLUMNS["commands"]
    assert "boot_id" in legacy_db.EXPECTED_NEW_COLUMNS["devices"]
    assert "protocol" in legacy_db.EXPECTED_NEW_COLUMNS["devices"]
    assert "frame_id" in legacy_db.EXPECTED_NEW_COLUMNS["frames"]
    assert set(legacy_db.EXPECTED_NEW_TABLES) == {"schema_version", "frame_archives", "install_tokens"}


def test_json_columns_are_declared_for_every_table_that_needs_them() -> None:
    assert set(legacy_db.JSON_COLUMNS) <= set(legacy_db.LEGACY_DB_TABLES)
    assert "detail" in legacy_db.JSON_COLUMNS["audit"]
    assert "payload" in legacy_db.JSON_COLUMNS["commands"]
    assert "settings" in legacy_db.JSON_COLUMNS["devices"]


@pytest.mark.skip(reason="W3: 需要 core/db.py 的迁移函数 + 0.2.10 真实库/合成数据")
def test_n3_migrates_legacy_schema_with_backup(tmp_dir) -> None:
    """迁移真实旧库：备份 → 六表保留 → 新列/新表出现 → 幂等可重复执行。"""

    raise AssertionError("W3: 见 tests/acceptance/test_n1_n4.py::test_n3_migration_from_real_0_2_10_database")