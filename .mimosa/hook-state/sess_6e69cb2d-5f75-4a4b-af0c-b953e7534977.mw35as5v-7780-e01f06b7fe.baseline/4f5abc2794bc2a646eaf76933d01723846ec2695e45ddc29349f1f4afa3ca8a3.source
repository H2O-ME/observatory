"""N2：0.2.x legacy 端点按 C12 删净，真实安装链路 enroll→token 有效。

N1/N3/N4 原先是"测试调用测试"的转发壳（N1 断言另一个测试文件的常量、N3/N4 直接调用
其他层的测试函数），既没有独立证据，也让同一份断言被跑两遍；已删除，由各层自己的
用例承担：N1 = ``tests/acceptance`` 全绿，N3 = ``tests/contract/test_legacy_db_schema.py``，
N4 = ``tests/perf/test_baseline.py``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.acceptance


def test_n2_legacy_surface_removed_enroll_roundtrip(tmp_dir: Path) -> None:
    """0.2.x 端点已按 C12 删净；真实安装链路 enroll→token 有效。

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
            # P4：部署票据与其管理接口整体移除（开放注册不需要准入票据）。
            ("GET", "/api/v1/install-tokens"),
            ("POST", "/api/v1/install-tokens"),
        ]
        token = enroll_token(client)
        for method, path in legacy_paths:
            response = getattr(client, method.lower())(path, headers={"X-Device-Token": token})
            assert response.status_code in (404, 405), (
                f"{method} {path} 应已删除（404/405），实际 {response.status_code}"
            )

        # P2 重新引入的端点：同名路径但语义不同（v3 卸载回执，需设备凭证）。
        # 它不是 0.2.x 的"提交卸载结果"接口，所以这里断言它要求认证而非 404。
        ack = client.post("/api/v1/agent/uninstall/ack", json={"ok": True})
        assert ack.status_code == 401, "新 ACK 端点必须存在且要求设备凭证"

        # 共享注册码不是凭据：开放注册下 code 被忽略；一旦配置部署密钥，错误
        # code 必须被拒（且失败时不得留下设备行）。
        open_mode = client.post("/api/v1/agent/enroll", json={"uid": "OBS-CODE-TRY", "code": "anything"})
        assert open_mode.status_code == 200, "开放注册下 code 不参与准入"

        settings = client.app.state.settings
        saved_key = settings.enroll_key
        try:
            settings.enroll_key = "expected-deployment-key"
            denied = client.post(
                "/api/v1/agent/enroll", json={"uid": "OBS-CODE-TRY-2", "code": "anything"}
            )
            assert denied.status_code == 401, denied.text
            assert "key_required" in denied.text
            assert "token" not in denied.json()
        finally:
            settings.enroll_key = saved_key