"""N1–N4 非功能验收（spec §3.2）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skip(reason="W3: N1 要求 A1–A24 全绿；当前仅 A22 等 W3 项未绿")
def test_n1_all_24_acceptance_tests_pass() -> None:
    """N1：A1..A24 全部有测试且通过（本文件之外，由 CI 的 acceptance job 汇总）。"""

    raise AssertionError("W3: 依赖前序波次，重新启用时不能带 skip")


def test_n2_legacy_zero_reinstall_upgrade(tmp_dir: Path) -> None:
    """N2：0.2.10 旧协议注册→收更新→校验→替换→以新协议重连（真跑）。"""

    from tests.contract.test_legacy_v2 import test_n2_zero_reinstall_upgrade as n2_replay

    n2_replay(tmp_dir)


def test_n3_migration_from_real_0_2_10_database(tmp_dir: Path) -> None:
    """N3：500 设备 / 20000 帧合成旧库迁移（备份、新列、幂等），见 contract 测试。"""

    from tests.contract.test_legacy_db_schema import test_n3_migrates_legacy_schema_with_backup

    test_n3_migrates_legacy_schema_with_backup(tmp_dir)


def test_n4_performance_not_regressed() -> None:
    """N4：同轮配对的转码耗时/体积/RSS 不退化（见 tests/perf/baseline.md）。"""

    from tests.perf.test_baseline import (
        test_n4_normalize_paired_not_regressed,
        test_n4_rss_idle_and_media_delta,
        test_n4_thumbnail_paired_not_regressed,
    )

    test_n4_normalize_paired_not_regressed()
    test_n4_thumbnail_paired_not_regressed()
    test_n4_rss_idle_and_media_delta()