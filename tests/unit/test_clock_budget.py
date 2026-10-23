"""超时预算（spec §5.4，A8）：一次写死，服务端 deadline < 客户端超时。"""

from __future__ import annotations

import pytest

from observatory.console.dto import budget_ms, with_deadline
from observatory.core.clock import CLIENT_TIMEOUT_GRACE_MS, client_timeout_s, server_deadline_ms
from observatory.core.config import Settings

pytestmark = pytest.mark.unit

SERVER_CAP_MS = 25_000


def test_budget_table_is_centralised() -> None:
    cfg = Settings()
    assert cfg.max_command_deadline_ms == SERVER_CAP_MS
    # §5.4：服务端 deadline = min(命令类型上限, 25s)
    assert server_deadline_ms("files", cfg) == SERVER_CAP_MS
    assert server_deadline_ms("update", cfg) == SERVER_CAP_MS  # min(60s, 25s)
    assert server_deadline_ms("uninstall", cfg) == SERVER_CAP_MS
    assert server_deadline_ms("sysinfo", cfg) == SERVER_CAP_MS
    # 其余默认 20s，未触顶
    assert server_deadline_ms("screenshot", cfg) == 20_000
    assert server_deadline_ms("not-a-real-command", cfg) == 20_000


def test_client_timeout_always_exceeds_server_deadline() -> None:
    cfg = Settings()
    for kind in ("files", "update", "uninstall", "sysinfo", "screenshot", "log_tail"):
        server = server_deadline_ms(kind, cfg)
        client = client_timeout_s(kind, cfg)
        assert client == pytest.approx((server + CLIENT_TIMEOUT_GRACE_MS) / 1000.0)
        assert client > server / 1000.0


def test_console_response_carries_duration_budget_not_epoch() -> None:
    stamped = with_deadline({"ok": True}, "files")
    assert isinstance(stamped["server_time_ms"], int) and stamped["server_time_ms"] > 0
    assert stamped["deadline_ms"] == budget_ms("files")
    assert 0 < stamped["deadline_ms"] <= SERVER_CAP_MS, "deadline_ms 是相对毫秒，不是 epoch"


def test_journal_and_session_derive_budget_from_clock() -> None:
    """命令创建与 session.request 都必须问 core.clock 要预算（禁止各接口自定）。"""

    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "server/observatory"
    journal = (root / "link/journal.py").read_text(encoding="utf-8")
    session = (root / "link/session.py").read_text(encoding="utf-8")
    assert "server_deadline_ms" in journal and "deadline_for" in journal
    assert "server_deadline_ms" in session and "client_timeout_s" in session
    # 禁止旧的 per-module 魔法数字式超时（如 timeout=20/25 秒硬编码在请求处）
    assert "timeout_s=20" not in session