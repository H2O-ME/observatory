"""纯函数测试：版本一致性（A19 的静态部分，W0 即可运行）。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

#: 与 tools/set_version.py 中的文档规则保持一致（A19）
README_VERSION = re.compile(r"当前版本：\*\*([0-9]+\.[0-9]+\.[0-9]+)\*\*")
DOC_HEADING_VERSION = re.compile(r"^#\s+.*（Observatory\s+([0-9]+\.[0-9]+\.[0-9]+)）", re.MULTILINE)
DOC_HEADER_VERSION = re.compile(r"^>\s*版本：([0-9]+\.[0-9]+\.[0-9]+)", re.MULTILINE)
VERSION_PLACEHOLDER = re.compile(r"\{\{\s*VERSION\s*\}\}")
#: 历史快照文档永久保留审计时的 0.2.10/0.3.0 约定，不参与漂移检查
HISTORICAL_DOCS = frozenset({"项目通读报告.md", "0.3.0-重构规格.md"})


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
    assert _fallback_of(REPO_ROOT / "server/observatory/core/config.py") == version
    assert _fallback_of(REPO_ROOT / "agent/observatory_agent/version.py") == version


def test_webui_package_version_matches() -> None:
    version = _read(REPO_ROOT / "VERSION").strip()
    package = json.loads(_read(REPO_ROOT / "webui/package.json"))
    assert package["version"] == version


def test_webui_package_lock_version_matches() -> None:
    """A19：package-lock 的顶层 + packages[""] 版本跟随 package.json。"""

    version = _read(REPO_ROOT / "VERSION").strip()
    lock = json.loads(_read(REPO_ROOT / "webui/package-lock.json"))
    assert lock["version"] == version
    assert lock["packages"][""]["version"] == version


def test_set_version_targets_cover_the_copies() -> None:
    """set_version.py 的同步目标必须覆盖上面断言的所有副本（防止加了副本忘了同步）。"""

    source = _read(REPO_ROOT / "tools/set_version.py")
    for fragment in (
        "server/observatory/core/config.py",
        "agent/observatory_agent/version.py",
        "webui/package.json",
        "webui/package-lock.json",
    ):
        assert fragment.split("/")[-1] in source or fragment in source, fragment
    # TARGETS / PACKAGE_JSONS 结构仍在（W3 可能改为生成式，届时改测试并保留 --check 行为）
    assert "TARGETS" in source and "PACKAGE_JSONS" in source and "PACKAGE_LOCK" in source


def test_set_version_check_covers_readme_and_docs() -> None:
    """A19：--check 必须覆盖 README 与 docs/*.md（release-ops 已把文档切到 0.3.0）。"""

    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools/set_version.py"), "--check"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "README.md" in completed.stdout
    assert "docs/*.md" in completed.stdout
    assert "package-lock.json" in completed.stdout
    source = _read(REPO_ROOT / "tools/set_version.py")
    assert "README_VERSION" in source and "DOC_HEADING_VERSION" in source
    assert "HISTORICAL_DOCS" in source
    assert "0.3.0-重构规格.md" in source, "历史契约快照不能随 VERSION 自动 bump"
    assert "项目通读报告.md" in source


def test_readme_and_docs_state_current_version() -> None:
    version = _read(REPO_ROOT / "VERSION").strip()

    readme = _read(REPO_ROOT / "README.md")
    found = README_VERSION.findall(readme)
    assert found, "README.md 缺少“当前版本：**X.Y.Z**”声明"
    assert set(found) == {version}, found

    for path in sorted((REPO_ROOT / "docs").glob("*.md")):
        if path.name in HISTORICAL_DOCS:
            continue
        text = _read(path)
        for pattern in (DOC_HEADING_VERSION, DOC_HEADER_VERSION):
            for declared in pattern.findall(text):
                assert declared == version, f"{path.name}: 文档版本 {declared} != {version}"


def test_no_unrendered_version_placeholder() -> None:
    targets = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]
    for path in targets:
        assert not VERSION_PLACEHOLDER.search(_read(path)), f"{path.name} 仍有 {{{{VERSION}}}} 占位符"