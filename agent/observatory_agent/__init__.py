"""Observatory agent package."""

from __future__ import annotations

from .version import resolve_version

__version__ = resolve_version()
PRODUCT = "Observatory"

__all__ = ["PRODUCT", "__version__"]