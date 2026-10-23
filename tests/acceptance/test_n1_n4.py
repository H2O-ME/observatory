"""N1–N4 非功能验收（spec §3.2）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_n1_all_24_acceptance_tests_pass() -> None:
    """N1：A1..A24 全部激活、无 skip；通过与否由同一次 CI 的 acceptance job 汇总。"""

    from tests.acceptance import test_definition_of_done as dod  # noqa: PLC0415

    assert dod.ACTIVE_NOW == frozenset(range(1, 25)), "24 条验收必须全部激活"
    for name, obj in vars(dod).items():
        if not (name.startswith("test_a") and callable(obj)):
            continue
        marks = getattr(obj, "pytestmark", []) or []
        unconditional = [m for m in marks if getattr(m, "name", "") == "skip" and not m.args]
        assert not unconditional, f"{name} 仍带无条件 skip"


def test_n2_legacy_zero_reinstall_upgrade(tmp_dir: Path) -> None:
    """N2：0.2.10 旧协议注册→收更新→校验→替换→以新协议重连（真跑）。"""

    from tests.contract.test_legacy_v2 import test_n2_zero_reinstall_upgrade as n2_replay

    n2_replay(tmp_dir)


def test_n3_migration_from_real_0_2_10_database(tmp_dir: Path) -> None:
    """N3：500 设备 / 20000 帧合成旧库迁移（备份、新列、幂等），见 contract 测试。"""

    from tests.contract.test_legacy_db_schema import test_n3_migrates_legacy_schema_with_backup

    test_n3_migrates_legacy_schema_with_backup(tmp_dir)


@pytest.mark.perf
def test_n4_performance_not_regressed() -> None:
    """N4：new vs 本机记录基线 ≤+10%（绝对毫秒不可跨机比较；默认回归不跑 perf）。"""

    from tests.perf.test_baseline import (
        test_n4_normalize_not_regressed,
        test_n4_rss_idle_and_media_delta,
        test_n4_thumbnail_not_regressed,
    )

    test_n4_normalize_not_regressed()
    test_n4_thumbnail_not_regressed()
    test_n4_rss_idle_and_media_delta()