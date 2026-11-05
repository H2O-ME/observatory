"""LinkRegistry 单一会话类型约束（C1）。"""

from __future__ import annotations

import asyncio

import pytest

from observatory.link.registry import LinkRegistry

pytestmark = pytest.mark.unit


class DuckSession:
    """0.2.x 风格 duck session：没有 clock/outbox，必须被 registry 拒绝。"""

    carrier = "http"


def test_registry_rejects_non_agent_session() -> None:
    registry = LinkRegistry()
    with pytest.raises(TypeError):
        asyncio.run(registry.register(1, DuckSession()))