"""presence 状态矩阵与 capabilities（spec §4.1，A12/A13/A15）。

唯一在线判定在 ``presence.PresenceContext.snapshot``；这里用固定时钟 + 假 registry
把阈值与 capabilities 的联合推导钉死。
"""

from __future__ import annotations

from typing import Any

import pytest

from observatory.core.config import Settings
from observatory.presence import (
    STATE_DEGRADED,
    STATE_OFFLINE,
    STATE_ONLINE,
    STATE_UNINSTALLED,
    TRANSPORT_NONE,
    TRANSPORT_POLL,
    TRANSPORT_WS,
    DeviceState,
    PresenceContext,
)

pytestmark = pytest.mark.unit

NOW = 1_000_000_000_000


class FixedClock:
    def __init__(self, now: int = NOW) -> None:
        self._now = now

    def now_ms(self) -> int:
        return self._now


class FakeSession:
    def __init__(self, carrier: str, last_seen_ms: int, protocol: int = 3) -> None:
        self.carrier = carrier
        self.last_seen_ms = last_seen_ms
        self.connected_at_ms = last_seen_ms
        self.protocol = protocol


class FakeRegistry:
    def __init__(self, sessions: dict[int, FakeSession] | None = None) -> None:
        self._sessions = sessions or {}

    def get(self, device_id: int) -> FakeSession | None:
        return self._sessions.get(int(device_id))


def _context(sessions: dict[int, FakeSession] | None = None, **settings_overrides: Any) -> PresenceContext:
    settings = Settings(**settings_overrides)
    return PresenceContext(settings=settings, registry=FakeRegistry(sessions), clock=FixedClock())


def _device(**overrides: Any) -> DeviceState:
    defaults: dict[str, Any] = {
        "device_id": 1,
        "uid": "OBS-TEST",
        "last_seen_ms": NOW - 1_000,
        "agent_version": "0.3.0",
        "protocol": 3,
        "settings": {},
        "capture": {"state": "ok", "at_ms": NOW},
    }
    defaults.update(overrides)
    return DeviceState(**defaults)


def test_online_ws_capabilities() -> None:
    context = _context({1: FakeSession("ws", NOW - 1_000)})
    snapshot = context.snapshot(_device())
    assert snapshot["state"] == STATE_ONLINE
    assert snapshot["transport"] == TRANSPORT_WS
    capabilities = snapshot["capabilities"]
    assert capabilities == {
        "control": True,
        "files": True,
        "screenshot": True,
        "timeline": True,
        "update": False,  # 没有新包
        "uninstall": True,
    }


def test_control_disabled_and_no_channel() -> None:
    context = _context({1: FakeSession("ws", NOW - 1_000)})
    disabled = context.snapshot(_device(settings={"control_enabled": False}))
    assert disabled["capabilities"]["control"] is False
    assert disabled["capabilities"]["files"] is True

    # 没有活动链路，但 offline_after 内仍有上报 → degraded，不能操作
    no_channel = _context().snapshot(_device(last_seen_ms=NOW - 30_000))
    assert no_channel["state"] == STATE_DEGRADED
    assert no_channel["transport"] == TRANSPORT_NONE
    assert no_channel["capabilities"]["files"] is False
    assert no_channel["capabilities"]["control"] is False
    assert no_channel["capabilities"]["uninstall"] is False


def test_poll_channel_is_online_but_not_controllable() -> None:
    context = _context({1: FakeSession("http", NOW - 1_000)})
    snapshot = context.snapshot(_device())
    assert snapshot["state"] == STATE_ONLINE
    assert snapshot["transport"] == TRANSPORT_POLL
    assert snapshot["capabilities"]["files"] is True
    assert snapshot["capabilities"]["control"] is False, "控制只允许实时 WS 载体"


def test_update_capability_requires_newer_package_and_upgradeable_base() -> None:
    context = _context({1: FakeSession("ws", NOW - 1_000)})
    assert context.snapshot(_device())["capabilities"]["update"] is False

    context.latest_agent_version = "0.3.1"
    current = context.snapshot(_device(agent_version="0.3.0"))
    assert current["capabilities"]["update"] is True
    assert current["self_update_blocked"] is None

    too_old = context.snapshot(_device(agent_version="0.2.4"))
    assert too_old["capabilities"]["update"] is False, "0.2.4 无法自更新（MIN_SELF_UPDATE_VERSION=0.2.5）"
    assert too_old["self_update_blocked"], "不可自更新必须给出明确原因"


def test_capture_blocked_disables_screenshot_and_control() -> None:
    context = _context({1: FakeSession("ws", NOW - 1_000)})
    snapshot = context.snapshot(_device(capture={"state": "no_session", "at_ms": NOW}))
    assert snapshot["capabilities"]["screenshot"] is False
    assert snapshot["capabilities"]["control"] is False
    assert "没有已登录的交互式桌面" in snapshot["reason"]


def test_agent_offline_after_s_changes_state() -> None:
    """A15：阈值必须来自 Settings，且改配置真的改变状态。"""

    last_seen = NOW - 100_000
    lenient = _context(**{"agent_offline_after_s": 120}).snapshot(_device(last_seen_ms=last_seen))
    strict = _context(**{"agent_offline_after_s": 60}).snapshot(_device(last_seen_ms=last_seen))
    assert lenient["state"] == STATE_DEGRADED
    assert strict["state"] == STATE_OFFLINE


def test_uninstalled_is_terminal_for_capabilities() -> None:
    context = _context({1: FakeSession("ws", NOW - 1_000)})
    snapshot = context.snapshot(_device(uninstalled_at_ms=NOW - 1))
    assert snapshot["state"] == STATE_UNINSTALLED
    capabilities = snapshot["capabilities"]
    assert capabilities["files"] is False
    assert capabilities["control"] is False
    assert capabilities["update"] is False
    assert capabilities["uninstall"] is False
    assert capabilities["timeline"] is True, "历史时间线不依赖在线状态"