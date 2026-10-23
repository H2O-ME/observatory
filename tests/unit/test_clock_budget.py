"""超时预算（spec §5.4，A8）：一次写死，服务端 deadline < 客户端超时。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

SERVER_CAP_S = 25
CLIENT_SLACK_S = 5


@pytest.mark.skip(reason="W1: 等待 observatory/core/clock.py 的 deadline_for()")
def test_deadline_table_is_centralised() -> None:
    from observatory.core.clock import deadline_for  # noqa: PLC0415

    assert deadline_for("files") == 25_000
    assert deadline_for("update") == 25_000  # min(60s, 25s)
    assert deadline_for("uninstall") == 25_000
    assert deadline_for("sysinfo") == 25_000
    assert deadline_for("screenshot") == 20_000  # 其余默认 20s，未触顶
    assert deadline_for("unknown-command") == 20_000


@pytest.mark.skip(reason="W1: 等待 core/clock.py 的 client_timeout_for()")
def test_client_timeout_always_exceeds_server_deadline() -> None:
    from observatory.core.clock import client_timeout_for, deadline_for  # noqa: PLC0415

    for command_type in ("files", "update", "uninstall", "sysinfo", "screenshot", "log_tail"):
        assert client_timeout_for(command_type) == deadline_for(command_type) + CLIENT_SLACK_S * 1000
        assert client_timeout_for(command_type) > deadline_for(command_type)


@pytest.mark.skip(reason="W1: 响应 DTO 统一附加 deadline_ms（console/dto.py）")
def test_every_response_carries_deadline_ms() -> None:
    from observatory.console.dto import with_clock  # noqa: PLC0415

    payload = with_clock({"ok": True})
    assert "server_time_ms" in payload
    assert "deadline_ms" in payload
    assert payload["deadline_ms"] > 0