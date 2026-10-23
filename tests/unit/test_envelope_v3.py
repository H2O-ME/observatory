"""协议 v3 信封单元测试（spec §5.1/§5.3）。W1 起启用。"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.unit


def _envelope():
    from observatory.link import envelope  # noqa: PLC0415

    return envelope


@pytest.mark.skip(reason="W1: 等待 server/observatory/link/envelope.py")
def test_envelope_roundtrip() -> None:
    envelope = _envelope()
    body = {"agent_version": "0.3.0", "state": {"transport": "ws"}}
    blob = envelope.encode("heartbeat", body, ts_ms=1_730_000_000_000)
    decoded = envelope.decode(blob)
    assert decoded["v"] == 3
    assert decoded["t"] == "heartbeat"
    assert decoded["body"] == body
    assert decoded["ts_ms"] == 1_730_000_000_000
    uuid.UUID(decoded["id"])  # id 必须是 uuid4


@pytest.mark.skip(reason="W1: 等待 link/envelope.py 的 id 注入与校验")
def test_envelope_id_is_unique_and_preserved() -> None:
    envelope = _envelope()
    first = envelope.decode(envelope.encode("ping", {}))
    second = envelope.decode(envelope.encode("ping", {}))
    assert first["id"] != second["id"]
    # 显式 id 必须保真（重投同一封信时 id 不变）
    fixed = envelope.decode(envelope.encode("ping", {}, msg_id="a" * 32))
    assert fixed["id"] == "a" * 32


@pytest.mark.skip(reason="W1: 未知 t 的忽略与可观测策略在 link/session.py 中实现")
def test_unknown_type_is_ignored_not_fatal() -> None:
    envelope = _envelope()
    decoded = envelope.decode(envelope.encode("future_type", {"x": 1}))
    session = __import__("observatory.link.session", fromlist=["AgentSession"])
    assert hasattr(session.AgentSession, "handle_envelope")
    assert decoded["t"] == "future_type"


@pytest.mark.skip(reason="W1: 上行 id 5 分钟 LRU 去重在 link/session.py")
def test_uplink_id_dedupes_within_window() -> None:
    from observatory.link.session import UplinkDeduper  # noqa: PLC0415

    deduper = UplinkDeduper(window_s=300)
    assert deduper.first_seen("id-1") is True
    assert deduper.first_seen("id-1") is False