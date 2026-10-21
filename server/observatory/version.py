"""Single source of the product version.

The repository root holds a plain-text ``VERSION`` file that is the only place a
human edits a version number. Both the server and the agent import the helpers
below so the console, the install script, the API and the Windows service all
report exactly the same string.

``FALLBACK_VERSION`` exists because a frozen agent (PyInstaller onedir) and a
slim server container may not ship the repository root. ``tools/set_version.py``
keeps the fallback in sync; a test asserts that they never drift.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "FALLBACK_VERSION",
    "VERSION_RE",
    "parse_version",
    "resolve_version",
    "version_tuple",
    "is_newer",
]

FALLBACK_VERSION = "0.2.7"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _candidate_roots() -> list[Path]:
    """Directories that may contain the authoritative VERSION file."""

    here = Path(__file__).resolve()
    return [
        here.parent,  # frozen agent: VERSION next to the exe
        here.parent.parent,  # repo/server
        here.parent.parent.parent,  # repo root
        Path.cwd(),
    ]


def resolve_version() -> str:
    """Return the authoritative version string.

    Search order: the first ``VERSION`` file found walking up from this module,
    then ``FALLBACK_VERSION``. Anything malformed is ignored so a stray file can
    never make the API report garbage.
    """

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
    """Parse ``MAJOR.MINOR.PATCH``; raise ``ValueError`` when malformed."""

    text = value.strip()
    if not VERSION_RE.match(text):
        raise ValueError(f"invalid version: {value!r}")
    major, minor, patch = text.split(".")
    return int(major), int(minor), int(patch)


def version_tuple(value: str | None = None) -> tuple[int, int, int]:
    return parse_version(value or resolve_version())


def is_newer(candidate: str, than: str | None = None) -> bool:
    """True when *candidate* sorts strictly after *than* (default: this build)."""

    try:
        other = parse_version(than) if than else version_tuple()
        return parse_version(candidate) > other
    except ValueError:
        return False


def main() -> int:  # pragma: no cover - convenience for shell use
    print(resolve_version())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())