"""Observatory control server."""

from __future__ import annotations

from .version import resolve_version

__version__ = resolve_version()
PRODUCT_NAME = "Observatory"

__all__ = ["__version__", "PRODUCT_NAME"]