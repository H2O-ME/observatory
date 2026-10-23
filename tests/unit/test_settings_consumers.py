"""A15：settings_spec 里的每个键都必须有真实消费者（禁止 placebo 配置）。

当前 5 个键的消费者属于上游任务（尚未落地），因此：
* ``test_no_new_placebo_settings`` 现在通过：缺消费者的集合必须 **不超过** 已知清单；
* ``test_all_settings_have_consumers`` 在上游落地前 skip（带 reason），落地后摘掉。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from observatory.core.settings_spec import SPECS, normalize

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "server/observatory"
AGENT = REPO_ROOT / "agent/observatory_agent"

#: 已知待消费键 → 责任任务（W1 结束前必须清空；当前已清零，保留结构防止再引入 placebo）
PENDING_CONSUMERS: dict[str, str] = {}


def _new_surface_files() -> list[Path]:
    files: list[Path] = []
    for directory in ("core", "link", "media", "console", "adapters"):
        files += list((SERVER / directory).rglob("*.py"))
    files += [SERVER / "presence.py", SERVER / "main.py", SERVER / "cli.py"]
    for directory in ("core", "win", "tasks"):
        files += list((AGENT / directory).rglob("*.py"))
    files += [AGENT / "main.py", AGENT / "version.py"]
    return [path for path in files if path.exists()]


def _consumed_keys() -> set[str]:
    texts = [
        path.read_text(encoding="utf-8", errors="ignore")
        for path in _new_surface_files()
        if path.name != "settings_spec.py"
    ]
    consumed: set[str] = set()
    for key in SPECS:
        if any(f'"{key}"' in text for text in texts):
            consumed.add(key)
    return consumed


def test_no_new_placebo_settings() -> None:
    missing = set(SPECS) - _consumed_keys()
    unexpected = missing - set(PENDING_CONSUMERS)
    assert not unexpected, f"新增了没有消费者的配置项: {sorted(unexpected)}"
    assert set(PENDING_CONSUMERS) >= missing


@pytest.mark.skip(reason="占位：消费者清零后本测试已由 test_all_settings_have_consumers 取代")
def test_pending_consumers_are_owned_and_tracked() -> None:
    missing = set(SPECS) - _consumed_keys()
    for key in missing:
        assert key in PENDING_CONSUMERS, f"{key} 缺消费者但未登记责任任务"
        assert PENDING_CONSUMERS[key].startswith("task-")


def test_all_settings_have_consumers() -> None:
    missing = set(SPECS) - _consumed_keys()
    assert missing == set(), f"仍有配置项没有消费者: {sorted(missing)}"


def test_every_spec_default_normalises() -> None:
    for key, spec in SPECS.items():
        assert normalize(key, spec.default) == spec.default, key
        assert spec.description, f"{key} 缺少说明"


def test_agent_offline_after_s_is_real_setting() -> None:
    from observatory.core.config import Settings

    assert Settings(agent_offline_after_s=42).offline_after_ms == 42_000
    assert Settings(agent_offline_after_s=120).offline_after_ms == 120_000


def test_agent_visible_and_secret_sets_are_disjoint() -> None:
    from observatory.core.settings_spec import AGENT_VISIBLE_KEYS, SECRET_KEYS

    assert AGENT_VISIBLE_KEYS.isdisjoint(SECRET_KEYS)
    assert "archive_token" in SECRET_KEYS
    assert "archive_token" not in AGENT_VISIBLE_KEYS