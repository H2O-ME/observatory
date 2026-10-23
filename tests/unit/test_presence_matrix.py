"""presence 状态矩阵与 capabilities（spec §4.1，A12/A13）。W1 起启用。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _presence():
    from observatory import presence  # noqa: PLC0415

    return presence


@pytest.mark.skip(reason="W1: 等待 observatory/presence.py 的 Settings 驱动阈值")
def test_thresholds_come_from_settings() -> None:
    from observatory.core.config import Settings  # noqa: PLC0415

    presence = _presence()
    settings = Settings(heartbeat_interval_s=20, poll_hold_s=20, agent_offline_after_s=120)
    assert presence.ws_stale_ms(settings) == 3 * 20_000
    assert presence.poll_stale_ms(settings) == 20_000 + 60_000
    assert presence.offline_after_ms(settings) == 120_000
    # 改配置必须真的改变阈值（A15 的一部分）
    assert presence.offline_after_ms(Settings(agent_offline_after_s=42)) == 42_000


@pytest.mark.skip(reason="W1: 等待 presence.capabilities() 单一函数")
def test_capability_matrix() -> None:
    presence = _presence()
    online = presence.capabilities(
        state="online",
        control_enabled=True,
        can_self_update=True,
        has_newer_package=True,
        capture_ok=True,
        has_channel=True,
    )
    assert online == {
        "control": True,
        "files": True,
        "screenshot": True,
        "timeline": True,
        "update": True,
        "uninstall": True,
    }
    # A13：设备级禁用与无升级包必须反映到 capability
    no_control = presence.capabilities(
        state="online",
        control_enabled=False,
        can_self_update=True,
        has_newer_package=True,
        capture_ok=True,
        has_channel=True,
    )
    assert no_control["control"] is False
    no_update = presence.capabilities(
        state="online",
        control_enabled=True,
        can_self_update=True,
        has_newer_package=False,
        capture_ok=True,
        has_channel=True,
    )
    assert no_update["update"] is False
    no_channel = presence.capabilities(
        state="degraded",
        control_enabled=True,
        can_self_update=True,
        has_newer_package=True,
        capture_ok=True,
        has_channel=False,
    )
    assert no_channel["files"] is False
    assert no_channel["control"] is False