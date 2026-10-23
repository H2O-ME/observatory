"""Link layer: one command path over two carriers.

``registry`` is the only place that maps a device to its live session;
``session`` implements the frozen public surface used by console and legacy;
``outbox`` is the carrier-independent downlink queue; ``journal`` is the single
command creation/result point with scoped waiters.
"""

from __future__ import annotations

__all__ = [
    "carrier_http",
    "carrier_ws",
    "envelope",
    "journal",
    "outbox",
    "registry",
    "session",
]