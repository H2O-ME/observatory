"""纯函数测试：版本一致性（A19 的静态部分，W0 即可运行）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _fallback_of(path: Path) -> str:
    match = re.search(r'^FALLBACK_VERSION\s*=\s*"([^"]+)"', _read(path), re.MULTILINE)
    assert match, f"{path} 缺少 FALLBACK_VERSION"
    return match.group(1)


def test_version_file_is_semver() -> None:
    version = _read(REPO_ROOT / "VERSION").strip()
    assert SEMVER.match(version), version
    assert version.startswith("0.3."), f"重构已切 0.3.0，VERSION={version}"


def test_fallback_copies_match_version() -> None:
    version = _read(REPO_ROOT / "VERSION").strip()
    assert _fallback_of(REPO_ROOT / "server/observatory/version.py") == version
    assert _fallback_of(REPO_ROOT / "agent/observatory_agent/version.py") == version


def test_webui_package_version_matches() -> None:
    version = _read(REPO_ROOT / "VERSION").strip()
    package = json.loads(_read(REPO_ROOT / "webui/package.json"))
    assert package["version"] == version


def test_set_version_targets_cover_the_copies() -> None:
    """set_version.py 的同步目标必须覆盖上面断言的所有副本（防止加了副本忘了同步）。"""

    source = _read(REPO_ROOT / "tools/set_version.py")
    for fragment in (
        "server/observatory/version.py",
        "agent/observatory_agent/version.py",
        "webui/package.json",
    ):
        assert fragment.split("/")[-1] in source or fragment in source, fragment
    # TARGETS / PACKAGE_JSONS 结构仍在（W3 可能改为生成式，届时改测试并保留 --check 行为）
    assert "TARGETS" in source and "PACKAGE_JSONS" in source