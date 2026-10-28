"""A23：默认域名只出现在一个配置模块，其余从配置读。

新架构层（core/link/media/console/adapters/presence/main/cli + agent 新层）里，
只有 ``core/config.py`` 允许持有生产域名；0.2.10 的顶层残留（``server/observatory/
config.py``、``server/observatory/hostsplit.py``）由 Lead 统一删除，但新层不得再
import 它们（否则 A23 失效）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "server/observatory"
AGENT = REPO_ROOT / "agent/observatory_agent"
DOMAINS = ("tianhw.top", "123423.xyz")


def _server_new_files() -> list[Path]:
    files: list[Path] = []
    for directory in ("core", "link", "media", "console", "adapters"):
        files += list((SERVER / directory).rglob("*.py"))
    files += [SERVER / "presence.py", SERVER / "main.py", SERVER / "cli.py"]
    return [path for path in files if path.exists()]


def _agent_new_files() -> list[Path]:
    files: list[Path] = []
    for directory in ("core", "win", "tasks"):
        files += list((AGENT / directory).rglob("*.py"))
    files += [AGENT / "main.py", AGENT / "version.py"]
    return [path for path in files if path.exists()]


def test_server_domains_only_in_core_config() -> None:
    allowed = SERVER / "core/config.py"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _server_new_files()
        if path != allowed and any(domain in path.read_text(encoding="utf-8") for domain in DOMAINS)
    ]
    assert offenders == [], f"新服务端层出现第二处默认域名: {offenders}"
    config = allowed.read_text(encoding="utf-8")
    assert "DEFAULT_CONSOLE_HOST" in config and "DEFAULT_AGENT_HOSTS" in config


def test_agent_domains_only_in_its_core_config() -> None:
    allowed = AGENT / "core/config.py"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _agent_new_files()
        if path != allowed and any(domain in path.read_text(encoding="utf-8") for domain in DOMAINS)
    ]
    assert offenders == [], f"新受控端层出现第二处默认域名: {offenders}"
    assert "builtin_base_urls" in allowed.read_text(encoding="utf-8").lower()


def test_no_second_hostsplit_policy_in_new_layer() -> None:
    """HostSplit 只允许 adapters/hostsplit.py 实现（C4/C8）。"""

    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _server_new_files()
        if path.name != "hostsplit.py" and re.search(r"class\s+HostSplit|X-Forwarded-Host", path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"重复实现了域名策略: {offenders}"


def test_webui_has_no_hardcoded_production_domains() -> None:
    webui = REPO_ROOT / "webui/src"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in webui.rglob("*")
        if path.suffix in {".ts", ".tsx"} and any(domain in path.read_text(encoding="utf-8") for domain in DOMAINS)
    ]
    assert offenders == [], f"前端出现硬编码生产域名: {offenders}"