"""HTTP long-poll carrier for the v3 link (``POST /api/v1/agent/link``).

The request carries an uplink batch and optionally holds for up to
``poll_hold_s`` (≤20 s) to return a downlink batch. It is semantically identical
to the WebSocket carrier: same envelopes, same session, same outbox.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Request

from ..core.clock import now_ms
from ..core.config import Settings
from ..core.crypto import DEVICE_TOKEN_HEADER
from ..core.errors import AppError
from ..core.log import log_event
from .agent_config import build_agent_config
from .agent_state import make_agent_report_reporter
from .envelope import UplinkBatch, decode_uplink_batch, encode_downlink_batch
from .journal import CommandJournal
from .limits import client_ip, enforce
from .registry import DeviceDirectory, LinkRegistry
from .session import AgentSession

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["link"])

#: Envelope types that make the HTTP request a long poll (nothing else to do).
_LONG_POLL_TYPES = frozenset({"heartbeat", "pong"})


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _registry(request: Request) -> LinkRegistry:
    return request.app.state.links


def _journal(request: Request) -> CommandJournal:
    return request.app.state.journal


def _directory(request: Request) -> DeviceDirectory:
    return request.app.state.device_directory


@router.post("/link")
async def link_http(
    request: Request,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    settings = _settings(request)
    registry = _registry(request)
    journal = _journal(request)
    directory = _directory(request)

    token = request.headers.get(DEVICE_TOKEN_HEADER, "").strip()
    ip = client_ip(request.headers, request.client, settings)
    try:
        enforce(
            getattr(request.app.state, "link_limiter", None),
            getattr(request.app.state, "link_ip_limiter", None),
            token,
            ip,
        )
    except AppError as exc:
        log_event(
            log,
            logging.WARNING,
            "link.http_rate_limited",
            ip=ip,
            retry_after=exc.retry_after,
            authenticated=bool(token),
        )
        raise
    if not token:
        raise AppError.auth("缺少设备令牌")
    # Invalid token → None → 401; uninstalled device raises AppError.gone → 410.
    identity = await directory.authenticate(token)
    if identity is None:
        raise AppError.auth("设备令牌无效")

    batch: UplinkBatch = decode_uplink_batch(payload)
    session = registry.get(identity.device_id)
    # A legacy adapter (protocol 2) is a valid AgentSession but not a v3 link:
    # replace it so this device gets the proper v3 outbox/command semantics.
    is_new = session is None or bool(getattr(session, "closed", False)) or session.protocol != batch.protocol
    stale = False
    if not is_new:
        stale = now_ms() - int(session.last_seen_ms or 0) > settings.poll_stale_ms
    if is_new:
        session = AgentSession(
            device_id=identity.device_id,
            uid=identity.uid,
            journal=journal,
            settings=settings,
            carrier="http",
            protocol=batch.protocol,
            agent_config=await build_agent_config(request.app, identity.settings),
            device_reporter=make_agent_report_reporter(request.app),
        )
        await registry.register(identity.device_id, session)
        replayed = await session.replay_pending()
        log_event(
            log,
            logging.INFO,
            "link.http.session_new",
            device_id=identity.device_id,
            uid=identity.uid,
            replayed=replayed,
        )
    elif stale:
        # The agent was offline longer than the poll window: refresh the safe
        # config and drain commands journaled while nobody was listening.
        session.agent_config = await build_agent_config(request.app, identity.settings)
        replayed = await session.replay_pending()
        log_event(
            log,
            logging.INFO,
            "link.http.resumed",
            device_id=identity.device_id,
            replayed=replayed,
        )
    session.note_carrier("http", batch.protocol)
    session.touch()

    if batch.last_ack_id:
        session.outbox.ack_up_to(batch.last_ack_id)

    replies = []
    for envelope in batch.items:
        if not session.accept_message(envelope.id):
            log_event(
                log,
                logging.DEBUG,
                "link.http.duplicate",
                device_id=identity.device_id,
                msg_id=envelope.id,
            )
            continue
        session.touch()
        replies.extend(await session.handle_uplink(envelope))

    # §5.6: an empty batch is a long poll; heartbeat/pong-only batches too. Any
    # other uplink (result/event) is answered immediately.
    hold_s = 0
    if not batch.items or all(item.t in _LONG_POLL_TYPES for item in batch.items):
        hold_s = settings.poll_hold_s

    downlink = replies + session.outbox.pending(limit=settings.outbox_max_messages)
    if not downlink and hold_s:
        downlink = await session.outbox.wait_for_messages(hold_s)

    # §5.6 / Lead ruling: `ack_id` is the **uplink** cursor — the last uplink id
    # the server has processed. The client advances its outbox with it. With no
    # uplink, echo the caller's downlink anchor so it is never lost.
    ack_id = batch.items[-1].id if batch.items else (batch.last_ack_id or "")
    log_event(
        log,
        logging.DEBUG,
        "link.http.exchange",
        device_id=identity.device_id,
        uplink=len(batch.items),
        downlink=len(downlink),
        hold_s=hold_s,
    )
    return encode_downlink_batch(downlink, ack_id=ack_id)


__all__ = ["router"]