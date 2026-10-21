"""Version helpers for the agent.

Shares the exact algorithm and ``FALLBACK_VERSION`` value with the server so the
console, the install script and the Windows service can never disagree. Kept as
a separate module (rather than importing the server package) because the agent is
frozen into its own onedir bundle.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

__all__ = ["FALLBACK_VERSION", "VERSION_RE", "parse_version", "resolve_version", "version_tuple", "is_newer"]

FALLBACK_VERSION = "0.2.3"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        # onedir bundle: the exe lives next to an optional VERSION file that the
        # updater writes so a freshly applied build reports its own version.
        roots.append(Path(sys.executable).resolve().parent)
    here = Path(__file__).resolve()
    roots.extend([here.parent, here.parent.parent, here.parent.parent.parent])
    roots.append(Path.cwd())
    return roots


def resolve_version() -> str:
    for root in _candidate_roots():
        candidate = root / "VERSION"
        try:
            text = candidate.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
        if VERSION_RE.match(text):
            return text
    return FALLBACK_VERSION


def parse_version(value: str) -> tuple[int, int, int]:
    text = value.strip()
    if not VERSION_RE.match(text):
        raise ValueError(f"invalid version: {value!r}")
    major, minor, patch = text.split(".")
    return int(major), int(minor), int(patch)


def version_tuple(value: str | None = None) -> tuple[int, int, int]:
    return parse_version(value or resolve_version())


def is_newer(candidate: str, than: str | None = None) -> bool:
    try:
        other = parse_version(than) if than else version_tuple()
        return parse_version(candidate) > other
    except ValueError:
        return False


if __name__ == "__main__":  # pragma: no cover
    print(resolve_version())