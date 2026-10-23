"""Console API router aggregation.

``main.py`` imports ``console_router`` and mounts it under ``/api/v1``; keeping
the aggregation here means the assembly layer never has to know the individual
modules.  ``router`` / ``api_router`` aliases exist so the W0 assembly can import
whichever name it settled on without a second wiring path.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    agent_frames,
    audit,
    auth,
    backup,
    control,
    devices,
    events,
    files,
    frames,
    install,
    settings,
    system,
    updates,
)

console_router = APIRouter()
console_router.include_router(agent_frames.router)
console_router.include_router(auth.router)
console_router.include_router(backup.router)
console_router.include_router(devices.router)
console_router.include_router(control.router)
console_router.include_router(files.router)
console_router.include_router(frames.router)
console_router.include_router(updates.router)
console_router.include_router(settings.router)
console_router.include_router(audit.router)
console_router.include_router(system.router)
console_router.include_router(install.router)
console_router.include_router(install.public_router)
console_router.include_router(events.router)

router = console_router
api_router = console_router

__all__ = ["api_router", "console_router", "router"]