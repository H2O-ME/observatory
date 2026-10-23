"""Observatory control server."""

from __future__ import annotations

from .core.config import PRODUCT, resolve_version

__version__ = resolve_version()
PRODUCT_NAME = PRODUCT

__all__ = ["__version__", "PRODUCT_NAME"]