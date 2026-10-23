"""N1–N4 非功能验收（spec §3.2）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skip(reason="W3: 需要 A1–A24 全绿后才能断言 N1")
def test_n1_all_24_acceptance_tests_pass() -> None:
    """N1：A1..A24 全部有测试且通过（本文件之外，由 CI 的 acceptance job 汇总）。"""

    raise AssertionError("W3: 依赖前序波次，重新启用时不能带 skip")


@pytest.mark.skip(reason="W1: 需要 legacy/agent_v2.py；回放骨架见 tests/contract/test_legacy_v2.py")
def test_n2_legacy_zero_reinstall_upgrade() -> None:
    """N2：0.2.10 旧协议注册→收更新→校验→替换→以新协议重连。"""

    raise AssertionError("W1: 见 tests/contract/test_legacy_v2.py::test_n2_zero_reinstall_upgrade")


@pytest.mark.skip(reason="W3: 需要迁移函数与 0.2.10 真实库（.localdata/observatory.db）")
def test_n3_migration_from_real_0_2_10_database() -> None:
    """N3：六表 + 历史画面平滑迁移；迁移前自动备份；失败可回滚。"""

    legacy_db = REPO_ROOT / ".localdata/observatory.db"
    if not legacy_db.exists():
        pytest.skip("0.2.10 真实库不存在（.localdata/observatory.db），N3 需在删除前保留样本")
    raise AssertionError("W3: 对真实库跑 migrate，断言备份文件与回滚")


@pytest.mark.skip(reason="W3: 需要 tools/bench.py 对新实现跑出对照表")
def test_n4_performance_not_regressed() -> None:
    """N4：采集 CPU/帧体积/内存不退化（对照 tests/perf/baseline.md）。"""

    raise AssertionError("W3: 跑 bench 与 baseline 阈值比较")