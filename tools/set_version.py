"""Bump the single authoritative version and sync every derived copy.

Usage::

    python tools/set_version.py 0.2.1     # set an explicit version
    python tools/set_version.py --bump    # increment the patch component
    python tools/set_version.py --check   # fail if any copy drifted

The repository root ``VERSION`` file wins. The two ``FALLBACK_VERSION`` constants
(server + agent) are rewritten to match so an offline build still reports the
right number, and the WebUI/agent package.json versions are kept in step.
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
    ROOT / "server" / "observatory" / "version.py",
    ROOT / "agent" / "observatory_agent" / "version.py",
]
PATTERN = re.compile(r'^FALLBACK_VERSION\s*=\s*"[^"]*"$', re.MULTILINE)

PACKAGE_JSONS = [
    ROOT / "webui" / "package.json",
]

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


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
    for problem in problems:
        print(f"DRIFT {problem}")
    if problems:
        print(f"\n{len(problems)} place(s) out of sync with VERSION={version}.")
        return 1
    print(f"VERSION={version} consistent everywhere.")
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