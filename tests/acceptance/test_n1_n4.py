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


def test_n2_legacy_surface_removed_enroll_roundtrip(tmp_dir: Path) -> None:
    """N2（0.3.1 改版）：0.2.x 端点已按 C12 删净；真实安装链路 enroll→token 有效。

    原语义“0.2.x 零重装升级”随共享注册码一并退役（现网设备已全部 0.3.0）；
    零重装能力由 v3 更新链路（update_offer + sha256 + 回滚）承接，见 A3/A7。
    """

    from tests.helpers import enroll_token, running_app

    with running_app(tmp_dir) as client:
        legacy_paths = [
            ("POST", "/api/v1/agent/heartbeat"),
            ("GET", "/api/v1/agent/commands"),
            ("POST", "/api/v1/agent/commands/x/result"),
            ("GET", "/api/v1/agent/state"),
            ("POST", "/api/v1/agent/uninstall/ack"),
        ]
        token = enroll_token(client)
        for method, path in legacy_paths:
            response = getattr(client, method.lower())(path, headers={"X-Device-Token": token})
            assert response.status_code in (404, 405), (
                f"{method} {path} 应已删除（404/405），实际 {response.status_code}"
            )

        # 注册码路径拒绝：只有一次性令牌能入网
        denied = client.post("/api/v1/agent/enroll", json={"uid": "OBS-CODE-TRY", "code": "anything"})
        assert denied.status_code == 401, denied.text


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