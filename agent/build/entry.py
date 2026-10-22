"""PyInstaller entry script.

Kept separate from ``observatory_agent/main.py`` so the bundle has a stable entry
point: when the Service Control Manager starts the executable it passes no
arguments, and ``main()`` with no arguments dispatches to the service.
"""

from __future__ import annotations

import multiprocessing
import sys


def _entry() -> int:
    # Frozen executables must call this before anything that could spawn a
    # process, or a child re-runs the whole bundle.
    multiprocessing.freeze_support()

    from observatory_agent.main import main as agent_main

    return agent_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(_entry())