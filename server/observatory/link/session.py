"""AgentSession — the frozen per-device link surface (C1/C3/C5).

One session exists per device regardless of carrier. It owns the downlink
outbox, the scoped command waiting, the uplink envelope handling and the control
frame fan-out. Console and legacy code talk to this object only.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from ..core.clock import Clock, SYSTEM_CLOCK, client_timeout_s, server_deadline_ms
from ..core.config import Settings, resolve_version
from ..core.errors import AppError
from ..core.log import log_event, redact
from .agent_state import DeviceReporter
from .envelope import (
    PROTOCOL_VERSION,
    Envelope,
    Result,
    new_envelope,
)
from .journal import CommandJournal
from .outbox import Outbox

log = logging.getLogger(__name__)

#: Bound queue between the agent's control frames and the browser viewer.
CONTROL_QUEUE_MAX = 4

CloseHook = Callable[[int, str], Awaitable[None]]
#: ``envelope -> written?`` — the WS carrier's immediate writer. Transient
#: envelopes use it to skip the reliable outbox entirely.
DirectSender = Callable[[Envelope], Awaitable[bool]]
#: ``(device_id, agent_version) -> offer dict | None`` — injected by the assembly
#: so link never imports console (A4). The provider owns the package table read.
UpdateOfferProvider = Callable[[int, str], Awaitable[dict[str, Any] | None]]
#: ``(kind, device_id, uid, body) -> None`` — uplink events the link cannot
#: persist itself (uninstall ack, agent events); the assembly routes them to
#: audit + console state. Failures are logged, never fatal to the link.
AgentEventSink = Callable[[str, int, str, dict[str, Any]], Awaitable[None]]

#: How often a session may ask the update-offer provider (heartbeat is 5-300s;
#: one small SELECT per minute per device is plenty for update propagation).
OFFER_RECHECK_MS = 60_000


@runtime_checkable
class SessionProtocol(Protocol):
    """The minimal frozen session interface (C1).

    ``registry`` and both carriers may rely **only** on this surface. There is
    exactly one implementation family: :class:`AgentSession`. Legacy 0.2.x
    carriers must subclass it and translate their wire shapes, never register a
    second duck-typed state machine (that is what caused the mixed-registry bug).
    """

    device_id: int
    uid: str
    carrier: str
    protocol: int
    last_seen_ms: int
    agent_config: dict[str, Any]
    clock: Clock
    closed: bool

    def touch(self) -> None: ...

    def note_carrier(self, carrier: str, protocol: int | None = None) -> None: ...

    async def send(self, kind: str, body: dict[str, Any], *, deadline_ms: int) -> str: ...

    async def request(
        self,
        kind: str,
        body: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> Result: ...

    async def replay_pending(self, limit: int = 50) -> int: ...

    async def close(self, *, code: int = 1000, reason: str = "") -> None: ...

    def open_control(self, token: str) -> ControlStream: ...

    def push_control_frame(self, token: str, payload: bytes) -> bool: ...


class ControlStream:
    """One browser viewer's frame queue for a control session token."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=CONTROL_QUEUE_MAX)
        self.closed = False

    def push(self, payload: bytes) -> bool:
        """Queue a frame, dropping the oldest when the viewer is slow."""

        if self.closed:
            return False
        while self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            return False
        return True

    def close(self) -> None:
        self.closed = True
        while self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            # Observable no-op: the viewer is already gone and will stop on close.
            log_event(log, logging.DEBUG, "control.stream_close_full", token=self.token)


class AgentSession:
    """The single live view of a device, independent of its carrier."""

    def __init__(
        self,
        *,
        device_id: int,
        uid: str,
        journal: CommandJournal,
        settings: Settings,
        clock: Clock | None = None,
        carrier: str = "http",
        protocol: int = PROTOCOL_VERSION,
        agent_config: Mapping[str, Any] | None = None,
        device_reporter: DeviceReporter | None = None,
        update_offer_provider: UpdateOfferProvider | None = None,
        event_sink: AgentEventSink | None = None,
    ) -> None:
        self.device_id = int(device_id)
        self.uid = str(uid)
        self.carrier = carrier
        self.protocol = int(protocol)
        self.journal = journal
        self.settings = settings
        self.clock = clock or SYSTEM_CLOCK
        #: Agent-safe settings projection computed outside link (A4): the carrier
        #: injects ``agent_view(SettingsStore.effective(...))`` so link never
        #: imports console and there is only one whitelist implementation.
        self.agent_config: dict[str, Any] = dict(agent_config or {})
        #: One DB write path for hello/heartbeat (agent_version/boot_id/protocol/
        #: last_seen_ms); carriers inject it, session never talks to the DB itself.
        self.device_reporter = device_reporter
        #: v3 update loop (F1): the provider turns agent_version into an offer.
        self._update_offer_provider = update_offer_provider
        self._event_sink = event_sink
        self._last_offer_check_ms = 0
        self.last_seen_ms = self.clock.now_ms()
        self.connected_at_ms = self.last_seen_ms
        self.agent_version = ""
        self.boot_id = ""
        self.last_uplink_id = ""
        self.outbox = Outbox(maxsize=settings.outbox_max_messages)
        self.closed = False

        self._send_lock = asyncio.Lock()
        self._close_hook: CloseHook | None = None
        #: Installed by the WS carrier only. Live-control input uses it instead
        #: of the outbox: at 30 Hz the bounded at-least-once queue filled with
        #: envelopes nobody acks, and every later command (files/drives) failed
        #: at enqueue with "下行队列已满" (reported 2026-10-27).
        self._direct_sender: DirectSender | None = None
        self._control_streams: dict[str, ControlStream] = {}
        self._seen: OrderedDict[str, int] = OrderedDict()
        self._stats: dict[str, int] = {
            "uplink": 0,
            "duplicate": 0,
            "unknown": 0,
            "control_frames": 0,
            "control_dropped": 0,
        }

    # -- state -------------------------------------------------------------
    def touch(self) -> None:
        self.last_seen_ms = self.clock.now_ms()

    def note_carrier(self, carrier: str, protocol: int | None = None) -> None:
        """Record the carrier; WS wins over HTTP when both are alive."""

        if carrier == "ws" or self.carrier != "ws":
            self.carrier = carrier
        if protocol is not None:
            self.protocol = int(protocol)

    def set_close_hook(self, hook: CloseHook | None) -> None:
        self._close_hook = hook

    def set_direct_sender(self, sender: DirectSender | None) -> None:
        """Install/clear the carrier's immediate (unacked) writer."""

        self._direct_sender = sender

    async def send_transient(self, kind: str, body: dict[str, Any]) -> bool:
        """Best-effort downlink that never occupies the reliable outbox.

        Only live-control input uses this. Those events are superseded by the
        next pointer move, so losing one is harmless; keeping them in the
        at-least-once outbox is not — they stay queued until an ack, and a
        30 Hz stream saturates the bounded queue within seconds, after which
        *every* command for that device fails at enqueue.
        """

        if self.closed:
            return False
        sender = self._direct_sender
        if sender is None:
            log_event(
                log,
                logging.WARNING,
                "session.transient_no_carrier",
                device_id=self.device_id,
                kind=kind,
            )
            return False
        envelope = new_envelope(kind, dict(body or {}), clock=self.clock)
        try:
            ok = bool(await sender(envelope))
        except Exception as exc:  # noqa: BLE001 - one dropped event must not end the viewer
            log_event(
                log,
                logging.DEBUG,
                "session.transient_failed",
                device_id=self.device_id,
                kind=kind,
                error=str(exc),
            )
            return False
        if not ok:
            log_event(
                log,
                logging.DEBUG,
                "session.transient_failed",
                device_id=self.device_id,
                kind=kind,
            )
        return ok

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # -- downlink ----------------------------------------------------------
    async def send(self, kind: str, body: dict[str, Any], *, deadline_ms: int) -> str:
        """Queue a downlink envelope and return its message id.

        ``deadline_ms`` is embedded for ``command`` envelopes (the agent needs it
        to enforce the budget); other envelope types carry their own body.
        """

        if self.closed:
            raise AppError.gone("链路已关闭，无法下发")
        payload = dict(body or {})
        if kind == "command":
            payload.setdefault("deadline_ms", int(deadline_ms))
        # Command envelopes reuse the command id as message id so an ack and a
        # result always refer to the same identifier on every carrier.
        msg_id = None
        if kind == "command" and isinstance(payload.get("command_id"), str):
            msg_id = payload["command_id"]
        envelope = new_envelope(kind, payload, msg_id=msg_id, clock=self.clock)
        self.outbox.put(envelope)
        log_event(
            log,
            logging.DEBUG,
            "session.sent",
            device_id=self.device_id,
            kind=kind,
            msg_id=envelope.id,
        )
        return envelope.id

    async def replay_pending(self, limit: int = 50) -> int:
        """Re-enqueue commands that were created while the device was offline.

        Called by both carriers when a session (re)appears. The command id is
        stable, so a command delivered twice is deduped by the agent (C3).
        """

        if self.closed:
            return 0
        records = await self.journal.replay_for_device(self.device_id, limit=limit)
        existing = {envelope.id for envelope in self.outbox.pending()}
        queued = 0
        for record in records:
            if record.id in existing:
                continue
            envelope = new_envelope(
                "command",
                record.to_envelope_body(deadline_ms=record.deadline_ms),
                msg_id=record.id,
                clock=self.clock,
            )
            self.outbox.put(envelope)
            queued += 1
        if queued:
            log_event(log, logging.INFO, "session.replayed", device_id=self.device_id, count=queued)
        return queued

    async def request(
        self,
        kind: str,
        body: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> Result:
        """Create a command, send it and wait for the correlated result.

        The waiter lives in an ``async with`` scope, so timeout, cancellation or
        an exception all remove it (A9). Timeout is a normal ``Result``, not an
        exception.
        """

        budget = server_deadline_ms(kind, self.settings)
        wait_for = float(timeout_s if timeout_s is not None else client_timeout_s(kind, self.settings))
        record = await self.journal.create(
            self.device_id,
            kind,
            body,
            actor="console",
            timeout_ms=budget,
        )
        started = self.clock.now_ms()
        async with self.journal.expect(record.id) as slot:
            try:
                await self.send(
                    "command",
                    record.to_envelope_body(deadline_ms=budget),
                    deadline_ms=budget,
                )
            except AppError as exc:
                await self.journal.settle_status(
                    record.id,
                    "cancelled",
                    error=exc.message,
                    error_code=exc.code.value,
                )
                return Result.failed(
                    command_id=record.id,
                    error=exc.message,
                    error_code=exc.code.value,
                    retryable=exc.retryable,
                    deadline_ms=budget,
                    server_time_ms=self.clock.now_ms(),
                    latency_ms=max(0, self.clock.now_ms() - started),
                )
            settled = await slot.wait(wait_for)

        if settled is not None:
            return settled

        # Budget exhausted: mark the command and stop redelivering it. If a result
        # slipped in between the wait timeout and this update, report that result
        # instead of losing it.
        settled = await self.journal.mark_timeout(record.id)
        self.outbox.ack(record.id)
        if settled is not None and settled.status in {"ok", "error"}:
            return Result(
                ok=settled.status == "ok",
                status=settled.status,
                result=settled.result,
                error=settled.error,
                error_code=settled.error_code,
                retryable=False,
                command_id=record.id,
                deadline_ms=settled.deadline_ms,
                server_time_ms=self.clock.now_ms(),
                latency_ms=max(0, self.clock.now_ms() - started),
            )
        log_event(
            log,
            logging.WARNING,
            "session.request_timeout",
            device_id=self.device_id,
            command_id=record.id,
            kind=kind,
            timeout_s=wait_for,
        )
        return Result.timed_out(
            command_id=record.id,
            error=f"受控端在 {wait_for:.0f} 秒内没有响应",
            deadline_ms=budget,
            server_time_ms=self.clock.now_ms(),
            latency_ms=max(0, self.clock.now_ms() - started),
        )

    # -- uplink ------------------------------------------------------------
    def accept_message(self, message_id: str) -> bool:
        """Uplink idempotency: ``False`` when the id was already seen (5 min LRU)."""

        now = self.clock.now_ms()
        ttl = self.settings.seen_cache_ttl_s * 1000
        while self._seen:
            oldest_at = next(iter(self._seen.values()))
            if len(self._seen) <= self.settings.seen_cache_max and now - oldest_at <= ttl:
                break
            self._seen.popitem(last=False)
        if message_id in self._seen:
            self._stats["duplicate"] += 1
            return False
        self._seen[message_id] = now
        while len(self._seen) > self.settings.seen_cache_max:
            self._seen.popitem(last=False)
        return True

    async def handle_uplink(self, envelope: Envelope) -> list[Envelope]:
        """Process one uplink envelope; returns immediate downlink replies."""

        self.touch()
        self._stats["uplink"] += 1
        self.last_uplink_id = envelope.id
        kind = envelope.t
        #: Any carrier that cannot piggyback the downlink cursor on a batch
        #: response (WS) reports it on every uplink envelope instead. Without
        #: this a long-lived WS link never acked a single non-command downlink,
        #: so `control` envelopes piled up until the outbox was full.
        self._ack_from_body(envelope.body)

        if kind == "hello":
            self._apply_agent_info(envelope.body)
            self.protocol = envelope.v
            await self._persist_device_report(envelope.body, envelope.v)
            return [await self._welcome()]

        if kind == "heartbeat":
            self._apply_agent_info(envelope.body)
            await self._persist_device_report(envelope.body, envelope.v)
            # F1: the only automatic update loop for v3 devices. The agent picks
            # the offer up from the reply batch on either carrier.
            offer = await self._maybe_update_offer()
            return [offer] if offer is not None else []

        if kind == "result":
            await self._handle_result(envelope.body)
            return []

        if kind == "event":
            log_event(
                log,
                logging.INFO,
                "session.agent_event",
                device_id=self.device_id,
                payload=redact({str(envelope.body.get("event") or "agent_event"): envelope.body}),
            )
            await self._emit("agent_event", envelope.body)
            return []

        if kind == "control_frame":
            self._handle_control_envelope(envelope.body)
            return []

        if kind == "uninstall_ack":
            log_event(log, logging.INFO, "session.uninstall_ack", device_id=self.device_id)
            # F2: persist uninstalled_at_ms + audit + revoke grants via the
            # injected sink (same facts the removed legacy endpoint wrote).
            await self._emit("uninstall_ack", envelope.body)
            return []

        if kind == "pong":
            return []

        self._stats["unknown"] += 1
        log_event(log, logging.WARNING, "session.unknown_uplink", device_id=self.device_id, kind=kind)
        return []

    async def on_binary(self, header: dict[str, Any], payload: bytes) -> None:
        """WS binary frames (v3 control frames)."""

        if header.get("t") != "ctrl":
            self._stats["unknown"] += 1
            log_event(log, logging.WARNING, "session.unknown_binary", device_id=self.device_id, header=redact(header))
            return
        token = header.get("session")
        if not isinstance(token, str) or not token:
            log_event(log, logging.WARNING, "control.frame_without_session", device_id=self.device_id)
            return
        self._stats["control_frames"] += 1
        self.push_control_frame(token, payload)

    # -- control streams ---------------------------------------------------
    def open_control(self, token: str) -> ControlStream:
        if not token:
            raise AppError.bad_request("缺少控制会话令牌")
        previous = self._control_streams.pop(token, None)
        if previous is not None:
            previous.close()
            log_event(log, logging.WARNING, "control.stream_replaced", token=token)
        stream = ControlStream(token)
        self._control_streams[token] = stream
        log_event(log, logging.INFO, "control.stream_opened", device_id=self.device_id, token=token)
        return stream

    def close_control(self, token: str) -> ControlStream | None:
        stream = self._control_streams.pop(token, None)
        if stream is not None:
            stream.close()
            log_event(log, logging.INFO, "control.stream_closed", device_id=self.device_id, token=token)
        return stream

    def push_control_frame(self, token: str, payload: bytes) -> bool:
        stream = self._control_streams.get(token)
        if stream is None or stream.closed:
            self._stats["control_dropped"] += 1
            log_event(
                log,
                logging.WARNING,
                "control.frame_dropped",
                device_id=self.device_id,
                token=token,
                reason="no active viewer",
            )
            return False
        return stream.push(payload)

    # -- lifecycle ---------------------------------------------------------
    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        self.outbox.close()
        for token in list(self._control_streams):
            self.close_control(token)
        hook = self._close_hook
        if hook is not None:
            try:
                await hook(code, reason)
            except Exception as exc:  # noqa: BLE001 - closing must not raise to callers
                log_event(log, logging.WARNING, "session.close_hook_failed", device_id=self.device_id, error=str(exc))
        log_event(log, logging.INFO, "session.closed", device_id=self.device_id, code=code, reason=reason)

    # -- internals ---------------------------------------------------------
    async def _emit(self, kind: str, body: dict[str, Any]) -> None:
        """Forward an uplink event to the assembly-provided sink (fail-open)."""

        sink = self._event_sink
        if sink is None:
            return
        try:
            await sink(kind, self.device_id, self.uid, dict(body or {}))
        except Exception as exc:  # noqa: BLE001 - a sink failure must not drop the link
            log_event(
                log,
                logging.WARNING,
                "session.event_sink_failed",
                device_id=self.device_id,
                kind=kind,
                error=str(exc),
            )

    async def _maybe_update_offer(self) -> Envelope | None:
        """Build an ``update_offer`` envelope when a newer package exists (F1).

        Gated by the device's effective ``auto_update`` setting (C9: the setting
        must have a real consumer) and throttled to one provider call per minute.
        Any provider failure degrades to "no offer" without touching the link.
        """

        provider = self._update_offer_provider
        if provider is None or self.closed:
            return None
        if not self.agent_config.get("auto_update", True):
            return None
        now = self.clock.now_ms()
        if now - self._last_offer_check_ms < OFFER_RECHECK_MS:
            return None
        self._last_offer_check_ms = now
        try:
            offer = await provider(self.device_id, self.agent_version)
        except Exception as exc:  # noqa: BLE001 - offer is best-effort
            log_event(
                log,
                logging.WARNING,
                "session.update_offer_failed",
                device_id=self.device_id,
                error=str(exc),
            )
            return None
        if not offer:
            return None
        log_event(
            log,
            logging.INFO,
            "session.update_offered",
            device_id=self.device_id,
            version=str(offer.get("version") or ""),
        )
        return new_envelope("update_offer", dict(offer), clock=self.clock)

    async def _welcome(self) -> Envelope:
        payload: dict[str, Any] = {
            "server_version": resolve_version(),
            "heartbeat_interval_s": self.settings.heartbeat_interval_s,
            "server_time_ms": self.clock.now_ms(),
            # Agent-safe projection built by the carrier via
            # core.settings_spec.agent_view (same function as console DTO).
            "config": dict(self.agent_config),
        }
        # Reset the throttle so a fresh connection always checks for an offer —
        # this is what pushes a fleet onto a newly uploaded package.
        self._last_offer_check_ms = 0
        offer = await self._maybe_update_offer()
        if offer is not None:
            payload["update_offer"] = offer.body
        return new_envelope("welcome", payload, clock=self.clock)

    def _apply_agent_info(self, body: dict[str, Any]) -> None:
        version = body.get("agent_version")
        if isinstance(version, str) and version:
            self.agent_version = version[:32]
        boot_id = body.get("boot_id")
        if isinstance(boot_id, str):
            self.boot_id = boot_id[:64]

    async def _persist_device_report(self, body: dict[str, Any], protocol: int) -> None:
        """Persist the report through the injected writer (single DB fact)."""

        if self.device_reporter is None:
            return
        try:
            await self.device_reporter(self.device_id, dict(body), int(protocol))
        except Exception as exc:  # noqa: BLE001 - a DB hiccup must not drop the link
            log_event(
                log,
                logging.ERROR,
                "session.device_report_failed",
                device_id=self.device_id,
                error=str(exc),
            )

    def _ack_from_body(self, body: dict[str, Any]) -> None:
        """Advance the **downlink** cursor (the agent's ``last_ack_id``)."""

        last_ack = body.get("last_ack_id")
        if isinstance(last_ack, str) and last_ack:
            self.outbox.ack_up_to(last_ack)

    def ack_envelope(self) -> Envelope | None:
        """Explicit carrier-level ack: ``ack_id`` = last processed **uplink** id."""

        if not self.last_uplink_id:
            return None
        return new_envelope(
            "pong",
            {"ack_id": self.last_uplink_id, "server_time_ms": self.clock.now_ms()},
            clock=self.clock,
        )

    async def _handle_result(self, body: dict[str, Any]) -> None:
        command_id = body.get("command_id") or body.get("id")
        if not isinstance(command_id, str) or not command_id:
            log_event(log, logging.WARNING, "session.result_without_id", device_id=self.device_id)
            return
        ok = bool(body.get("ok"))
        raw_result = body.get("result")
        if raw_result is not None and not isinstance(raw_result, dict):
            log_event(
                log,
                logging.WARNING,
                "session.result_not_object",
                device_id=self.device_id,
                command_id=command_id,
            )
        result = raw_result if isinstance(raw_result, dict) else None
        error = str(body.get("error") or "") or None
        raw_code = body.get("error_code")
        error_code = str(raw_code) if raw_code else (None if ok else "INTERNAL")
        elapsed_ms = body.get("elapsed_ms")
        if isinstance(elapsed_ms, int) and elapsed_ms >= 0:
            self._stats["last_elapsed_ms"] = elapsed_ms
        await self.journal.settle_result(
            command_id,
            ok=ok,
            result=result,
            error=error,
            error_code=error_code,
        )
        # A result proves delivery; stop redelivering that command. A late
        # duplicate result is idempotent in the journal (first result wins).
        self.outbox.ack(command_id)
        # F4: command status changed — tell the console hub (best-effort).
        await self._emit("command_updated", {"command_id": command_id, "ok": ok})

    def _handle_control_envelope(self, body: dict[str, Any]) -> None:
        token = body.get("session")
        data = body.get("data_b64")
        if not isinstance(token, str) or not token:
            log_event(log, logging.WARNING, "control.envelope_without_session", device_id=self.device_id)
            return
        if not isinstance(data, str) or not data:
            log_event(log, logging.WARNING, "control.envelope_without_data", device_id=self.device_id)
            return
        try:
            payload = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as exc:
            log_event(log, logging.WARNING, "control.envelope_bad_base64", device_id=self.device_id, error=str(exc))
            return
        self._stats["control_frames"] += 1
        self.push_control_frame(token, payload)


__all__ = ["AgentSession", "CONTROL_QUEUE_MAX", "ControlStream", "SessionProtocol"]