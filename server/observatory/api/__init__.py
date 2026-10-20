"""API router aggregation."""

from __future__ import annotations

from fastapi import APIRouter

from . import agent, agent_ws, auth, control, devices, files, frames, public, settings, updates, ws

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(devices.router)
api_router.include_router(control.router)
api_router.include_router(files.router)
api_router.include_router(frames.router)
api_router.include_router(updates.router)
api_router.include_router(settings.router)
api_router.include_router(agent.router)
api_router.include_router(agent_ws.router)
api_router.include_router(public.router)
api_router.include_router(public.public_router)
api_router.include_router(ws.router)

__all__ = ["api_router"]