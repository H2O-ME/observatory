"""Core building blocks: configuration, database, clock, crypto, errors, logging, models.

Only the application assembly (``observatory.main``) and the feature packages
(``link``, ``console``, ``media``, ``legacy``) import this package. Nothing in
``core`` imports a feature package, which keeps the dependency direction one-way.
"""

from __future__ import annotations

__all__ = ["__doc__"]