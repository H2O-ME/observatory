"""Bump the single authoritative version and sync every derived copy.

Usage::

    python tools/set_version.py 0.2.1     # set an explicit version
    python tools/set_version.py --bump    # increment the patch component
    python tools/set_version.py --check   # fail if any copy drifted

The repository root ``VERSION`` file wins. The two ``FALLBACK_VERSION`` constants
(server + agent) are rewritten to match so an offline build still reports the
right number, the WebUI/agent package.json versions are kept in step, and the
version statements in ``README.md`` / ``docs/*.md`` are checked and synced (A19).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"

TARGETS = [
    ROOT / "server" / "observatory" / "core" / "config.py",
    ROOT / "agent" / "observatory_agent" / "version.py",
]
PATTERN = re.compile(r'^FALLBACK_VERSION\s*=\s*"[^"]*"$', re.MULTILINE)

PACKAGE_JSONS = [
    ROOT / "webui" / "package.json",
]

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# ---------------------------------------------------------------------------
# 文档（A19）：--check 不只覆盖代码副本，也要覆盖 README 与 docs/*.md 的
# 版本声明 / 未渲染占位符，否则发版后文档会静默漂移。
# ---------------------------------------------------------------------------
README_PATH = ROOT / "README.md"
DOCS_DIR = ROOT / "docs"

#: README 的权威声明：`当前版本：**0.3.0**`
README_VERSION = re.compile(r"当前版本：\*\*([0-9]+\.[0-9]+\.[0-9]+)\*\*")
#: 文档标题里的版本：`# 部署（Observatory 0.3.0）`
DOC_HEADING_VERSION = re.compile(r"^#\s+.*（Observatory\s+([0-9]+\.[0-9]+\.[0-9]+)）", re.MULTILINE)
#: 文档顶部的版本块：`> 版本：0.3.0（...）`
DOC_HEADER_VERSION = re.compile(r"^>\s*版本：([0-9]+\.[0-9]+\.[0-9]+)", re.MULTILINE)
#: 未渲染的版本占位符：`{{VERSION}}`（发布前必须替换成字面版本）
DOC_PLACEHOLDER = re.compile(r"\{\{\s*VERSION\s*\}\}")
#: 历史快照文档永久保留审计时的版本号（0.2.10 通读报告、0.3.0 契约规格），
#: 不随 VERSION 漂移——它们是“当时约定”的证据，不是当前版本声明。
HISTORICAL_DOCS = frozenset({"项目通读报告.md", "0.3.0-重构规格.md"})

#: npm lockfile 的 version 字段跟随 package.json（顶层与 packages[""] 两处）。
PACKAGE_LOCK = ROOT / "webui" / "package-lock.json"


def _lock_versions(data: dict) -> list[str]:
    found: list[str] = []
    top = data.get("version")
    if isinstance(top, str):
        found.append(top)
    packages = data.get("packages")
    if isinstance(packages, dict):
        root = packages.get("")
        if isinstance(root, dict) and isinstance(root.get("version"), str):
            found.append(root["version"])
    return found


def _sync_lock(version: str) -> bool:
    if not PACKAGE_LOCK.exists():
        return False
    data = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    changed = False
    if data.get("version") != version:
        data["version"] = version
        changed = True
    root = data.get("packages", {}).get("") if isinstance(data.get("packages"), dict) else None
    if isinstance(root, dict) and root.get("version") != version:
        root["version"] = version
        changed = True
    if changed:
        PACKAGE_LOCK.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    return changed


def _doc_targets() -> list[Path]:
    targets = [README_PATH, *sorted(DOCS_DIR.glob("*.md"))]
    return [path for path in targets if path.exists()]


def _version_patterns(path: Path) -> list[re.Pattern[str]]:
    if path == README_PATH:
        return [README_VERSION]
    if path.name in HISTORICAL_DOCS:
        return []
    return [DOC_HEADING_VERSION, DOC_HEADER_VERSION]


def _check_docs(version: str) -> list[str]:
    problems: list[str] = []
    readme = README_PATH.read_text(encoding="utf-8")
    readme_matches = README_VERSION.findall(readme)
    if not readme_matches:
        problems.append("README.md: 缺少“当前版本：**X.Y.Z**”声明")
    for found in readme_matches:
        if found != version:
            problems.append(f"README.md: 当前版本 {found} != {version}")

    for path in _doc_targets():
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT)
        for pattern in _version_patterns(path):
            for found in pattern.findall(text):
                if found != version:
                    problems.append(f"{relative}: 文档版本 {found} != {version}")
        if DOC_PLACEHOLDER.search(text):
            problems.append(f"{relative}: 仍有未渲染的 {{{{VERSION}}}} 占位符")
    return problems


def _sync_docs(version: str) -> list[str]:
    changed: list[str] = []
    for path in _doc_targets():
        original = path.read_text(encoding="utf-8")
        updated = original
        for pattern in _version_patterns(path):
            updated = pattern.sub(lambda match: match.group(0).replace(match.group(1), version, 1), updated)
        updated = DOC_PLACEHOLDER.sub(version, updated)
        if updated != original:
            path.write_text(updated, encoding="utf-8", newline="\n")
            changed.append(str(path.relative_to(ROOT)))
    return changed


def read_root_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8-sig").strip()
    if not SEMVER.match(text):
        raise SystemExit(f"VERSION does not hold a semantic version: {text!r}")
    return text


def write_root_version(version: str) -> None:
    if not SEMVER.match(version):
        raise SystemExit(f"not a semantic version: {version!r}")
    # newline="\n" is essential: Path.write_text opens in text mode, so on Windows
    # the default translation would turn "\n" into "\r\n" and commit
    # "0.2.8\r\n". Shell scripts then read "0.2.8\r" via $(cat VERSION), which
    # silently breaks every string comparison (see .gitattributes).
    VERSION_FILE.write_text(version + "\n", encoding="utf-8", newline="\n")


def bump_patch(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def sync(version: str) -> list[str]:
    changed: list[str] = []
    for target in TARGETS:
        if not target.exists():
            continue
        original = target.read_text(encoding="utf-8")
        replacement = f'FALLBACK_VERSION = "{version}"'
        updated, count = PATTERN.subn(replacement, original)
        if count != 1:
            raise SystemExit(f"{target}: expected exactly one FALLBACK_VERSION assignment, found {count}")
        if updated != original:
            target.write_text(updated, encoding="utf-8", newline="\n")
            changed.append(str(target.relative_to(ROOT)))

    for pkg_path in PACKAGE_JSONS:
        if not pkg_path.exists():
            continue
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
        if data.get("version") != version:
            data["version"] = version
            pkg_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
            changed.append(str(pkg_path.relative_to(ROOT)))

    if _sync_lock(version):
        changed.append(str(PACKAGE_LOCK.relative_to(ROOT)))

    changed.extend(_sync_docs(version))
    return changed


def check() -> int:
    version = read_root_version()
    problems: list[str] = []
    for target in TARGETS:
        if not target.exists():
            continue
        text = target.read_text(encoding="utf-8")
        match = re.search(r'^FALLBACK_VERSION\s*=\s*"([^"]*)"$', text, re.MULTILINE)
        if not match:
            problems.append(f"{target.relative_to(ROOT)}: missing FALLBACK_VERSION")
        elif match.group(1) != version:
            problems.append(f"{target.relative_to(ROOT)}: {match.group(1)} != {version}")
    for pkg_path in PACKAGE_JSONS:
        if not pkg_path.exists():
            continue
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
        if data.get("version") != version:
            problems.append(f"{pkg_path.relative_to(ROOT)}: {data.get('version')} != {version}")
    if PACKAGE_LOCK.exists():
        lock_data = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
        for found in _lock_versions(lock_data):
            if found != version:
                problems.append(f"{PACKAGE_LOCK.relative_to(ROOT)}: {found} != {version}")
                break
    problems.extend(_check_docs(version))
    for problem in problems:
        print(f"DRIFT {problem}")
    if problems:
        print(f"\n{len(problems)} place(s) out of sync with VERSION={version}.")
        return 1
    print(f"VERSION={version} consistent everywhere.")
    print("checked: VERSION, FALLBACK_VERSION x2, webui/package.json, webui/package-lock.json, README.md, docs/*.md")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the product version")
    parser.add_argument("version", nargs="?", help="explicit version, e.g. 0.2.1")
    parser.add_argument("--bump", action="store_true", help="increment the patch component")
    parser.add_argument("--check", action="store_true", help="only verify that copies agree")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.check:
        return check()

    current = read_root_version()
    if args.bump:
        target_version = bump_patch(current)
    elif args.version:
        target_version = args.version.strip()
        if not SEMVER.match(target_version):
            raise SystemExit(f"not a semantic version: {target_version!r}")
    else:
        print(f"current version: {current}")
        return 0

    if args.dry_run:
        print(f"would set {current} -> {target_version}")
        return 0

    write_root_version(target_version)
    changed = sync(target_version)
    print(f"VERSION {current} -> {target_version}")
    for path in changed:
        print(f"  updated {path}")
    print("\nNext steps:")
    print("  python tools/set_version.py --check")
    print("  git add -A && git commit -m 'release: " + target_version + "' && git tag v" + target_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))