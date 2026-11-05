"""Agent-safe configuration projection injected into ``welcome.config`` (A4).

``link`` must never import ``console`` (console imports link), so the carrier
receives the finished dict from a provider installed by the assembly. The
provider calls the *same* function as the console DTO chain:
``core.settings_spec.agent_view`` (console/dto.device_safe_config delegates to
it), so there is exactly one whitelist implementation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Awaitable, Callable

from ..core.settings_spec import DEFAULT_SETTINGS, agent_view

log = logging.getLogger(__name__)

AgentConfigProvider = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]


def fallback_agent_config() -> dict[str, Any]:
    """Safe projection of server defaults (no DB, no secrets)."""

    return agent_view(dict(DEFAULT_SETTINGS))


async def build_agent_config(app: Any, device_settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """Resolve the agent-facing config through the injected provider.

    A provider/DB failure degrades to the *safe defaults* projection instead of
    blocking the link, and is always logged (never silently swallowed).
    """

    provider = getattr(app.state, "agent_config_provider", None)
    database = getattr(app.state, "db", None)
    if provider is None or database is None:
        return fallback_agent_config()
    try:
        async with database.sessionmaker() as session:
            config = await provider(session, dict(device_settings or {}))
    except Exception as exc:  # noqa: BLE001 - link must survive a settings read failure
        log.warning("agent config provider failed (%s); using safe defaults", exc)
        return fallback_agent_config()
    return dict(config or {})


__all__ = ["AgentConfigProvider", "build_agent_config", "fallback_agent_config"]