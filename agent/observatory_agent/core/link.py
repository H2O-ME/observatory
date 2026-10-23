"""唯一链路：WebSocket 与 HTTP 长轮询只是同一份协议的**两个载体**。

0.2.10 的受控端有六条上行路径（WS 心跳 / WS 回执 / WS 事件 / HTTP 心跳 /
HTTP 轮询 / HTTP 回执）外加独立的 ``spool`` 重传；同样的事实有多份实现，
"修一个 bug 多一个 bug"。0.3.0 只保留这一条逻辑链路（规格 C1）：

    Link.send(kind, body)  ->  outbox  ->  载体（ws | http）  ->  服务端
    Link.recv()            <-  载体（ws | http）  <-  服务端下行信封
    Link.enqueue_frame()   ->  outbox  ->  POST /api/v1/agent/frames（唯一帧上传路径，C2）

两条载体的语义完全一致：同一套信封、同一个 outbox、同一套幂等 id。切换载体
（WS 掉线 → HTTP 长轮询；HTTP 稳定后指数探测 WS）只改变字节怎么走，不改变
任何行为，也不重放已确认消息。

设计要点：
- **载体选优**：WS 优先；WS 连不上/断开立刻降级到 HTTP；HTTP 稳定后按
  120s→1800s 指数探测 WS，成功后切回。
- **重连退避**：以 ``CarrierPolicy`` 这个**纯状态机**表达（时间由调用方传入），
  单测可直接断言 2,5,10,20,30,60,120 与 120,240,480,960,1800 两个序列。
- **心跳是信封类型**，不是独立端点循环：``Link`` 内部的心跳任务只是
  ``send("heartbeat", …)``，与回执/事件走完全相同的路径。
- **401 必须重新注册**（A17 受控端侧）：任何一次 AUTH 失败都会调用
  ``on_unauthorized`` 回调拿新令牌，然后回主链路；拿不到就退避重试。
- **GONE（410 / WS 4002）是终态**：设备已被服务端标记卸载，链路停下并通知
  调用方，不再重连。

IO/并发边界：``Link`` 自己持有 asyncio 任务（载体/帧上传/心跳）。除
``enqueue_frame`` 外所有公开方法都必须在该事件循环内调用；其它线程投递帧需要
``loop.call_soon_threadsafe``。纯逻辑（``CarrierPolicy``）与 IO 分离，便于测试。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx

from .config import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    MAX_HEARTBEAT_INTERVAL_S,
    MIN_HEARTBEAT_INTERVAL_S,
    Clock,
    new_boot_id,
)
from .envelope import (
    BATCH_MAX_ITEMS,
    Envelope,
    EnvelopeError,
    build_uplink_batch,
    decode,
    decode_binary,
    encode_binary,
    is_known_type,
    parse_downlink_batch,
)
from .outbox import Outbox, OutboxEntry, OutboxFull, OutboxLimits

log = logging.getLogger(__name__)

DEVICE_TOKEN_HEADER: Final[str] = "X-Device-Token"

#: 协议端点（规格 §5.2）。
LINK_PATH: Final[str] = "/api/v1/agent/link"
FRAME_PATH: Final[str] = "/api/v1/agent/frames"
ENROLL_PATH: Final[str] = "/api/v1/agent/enroll"
WS_PATH: Final[str] = "/api/v1/agent/link"

#: 服务端 hold 20s，客户端超时 30s（规格 §5.3）。
HTTP_HOLD_S: Final[float] = 20.0
HTTP_TIMEOUT_S: Final[float] = 30.0
FRAME_TIMEOUT_S: Final[float] = 45.0
ENROLL_TIMEOUT_S: Final[float] = 30.0
CONNECT_TIMEOUT_S: Final[float] = 10.0

WS_OPEN_TIMEOUT_S: Final[float] = 12.0
WS_MAX_MESSAGE_BYTES: Final[int] = 16 * 1024 * 1024

#: WS 关闭码（W1 服务端语义）：4401 令牌失效需重注册；4410 设备已卸载（终态，
#: 兼容旧服务端的 4002）；4429 被限流（可重连退避）。HTTP 对应 401/410/429。
WS_CLOSE_UNAUTHORIZED: Final[int] = 4401
WS_CLOSE_GONE: Final[int] = 4410
WS_CLOSE_GONE_LEGACY: Final[int] = 4002
WS_CLOSE_RATE_LIMITED: Final[int] = 4429

#: WS 失败后的重连退避序列；最后一项封顶重复使用。
RECONNECT_DELAYS_S: Final[tuple[float, ...]] = (2, 5, 10, 20, 30, 60, 120)
#: HTTP 载体稳定后向上探测 WS 的间隔：120s 起 ×2，封顶 1800s。
PROBE_BASE_S: Final[float] = 120.0
PROBE_MAX_S: Final[float] = 1800.0
#: 帧上传失败的退避（帧走独立 HTTP 路径，不占用链路信封的退避）。
FRAME_RETRY_DELAYS_S: Final[tuple[float, ...]] = (1, 2, 5, 10, 30, 60)
#: HTTP 载体一轮若"秒回"，至少间隔这么久，避免空批把服务端打成短轮询。
MIN_HTTP_ROUND_S: Final[float] = 1.0
#: 下行信封队列容量：消费端（命令分发）卡住时给回压，不无限攒内存。
INBOX_MAX: Final[int] = 256
INBOX_PUT_TIMEOUT_S: Final[float] = 10.0
#: 401 后拿不到新令牌的等待时间（避免打爆注册端点）。
REAUTH_RETRY_S: Final[float] = 15.0
#: 未知下行类型的日志上限：超过后每个类型只记一次，避免刷屏（仍可观测）。
UNKNOWN_TYPE_LOG_LIMIT: Final[int] = 32


# ------------------------------------------------------------------ 异常 ---
class LinkError(RuntimeError):
    """链路层错误的基类。``retryable`` 决定调用方是否应该重试。"""

    retryable: bool = True


class LinkUnavailable(LinkError):
    """载体/网络不可用（可重试）。"""


class LinkAuthError(LinkError):
    """令牌被拒（401/403）；需要重新注册（A17）。"""

    retryable = False


class LinkGoneError(LinkError):
    """设备已被服务端标记卸载（410 / WS 4002）：终态，不再重连。"""

    retryable = False


class LinkBadRequest(LinkError):
    """请求被服务端判定为非法（4xx，除 401/403/410/429）；重试无意义。"""

    retryable = False

    def __init__(self, message: str, *, msg_ids: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        #: 触发这次拒绝的上行 id，便于调用方把它们从 outbox 里丢弃并记日志。
        self.msg_ids = msg_ids


class LinkRateLimited(LinkError):
    """被限流（429）；``retry_after_s`` 来自 ``Retry-After``。"""

    def __init__(self, message: str, *, retry_after_s: float = 5.0) -> None:
        super().__init__(message)
        self.retry_after_s = max(0.0, float(retry_after_s))


class LinkClosed(LinkError):
    """链路已关闭；承载任务的协程应正常退出。"""

    retryable = False


# ------------------------------------------------------- 纯退避状态机 ---
def reconnect_delays(count: int | None = None) -> tuple[float, ...]:
    """重连退避序列（纯函数，默认 2→120s）。"""

    if count is None:
        return RECONNECT_DELAYS_S
    if count <= 0:
        return ()
    if count <= len(RECONNECT_DELAYS_S):
        return RECONNECT_DELAYS_S[:count]
    return RECONNECT_DELAYS_S + (RECONNECT_DELAYS_S[-1],) * (count - len(RECONNECT_DELAYS_S))


def probe_delay(attempt: int) -> float:
    """第 ``attempt`` 次（0 起）WS 探测间隔（纯函数）：120,240,…,1800,1800…"""

    if attempt < 0:
        raise ValueError("attempt 不能为负")
    return min(PROBE_BASE_S * (2.0**attempt), PROBE_MAX_S)


def probe_delays(count: int) -> tuple[float, ...]:
    """连续 ``count`` 次探测的间隔序列（纯函数，便于单测断言）。"""

    if count <= 0:
        return ()
    return tuple(probe_delay(index) for index in range(count))


@dataclass
class CarrierPolicy:
    """载体升/降级的**纯**状态机：只存计数，不读时钟、不做 IO。

    调用方（``Link._carrier_loop`` / ``_run_http_phase``）在事件发生时喂给它
    "发生了一次失败/一次成功"，然后按返回的秒数安排下一次动作。这样退避逻辑
    独立于 asyncio，可直接单测。
    """

    reconnect_delays_s: tuple[float, ...] = RECONNECT_DELAYS_S
    probe_base_s: float = PROBE_BASE_S
    probe_max_s: float = PROBE_MAX_S
    _failure_index: int = field(default=0, init=False)
    _probe_attempt: int = field(default=0, init=False)

    def next_reconnect_delay(self) -> float:
        """网络失败后的等待秒数：2,5,10,20,30,60,120,120,…"""

        if not self.reconnect_delays_s:
            return 0.0
        delay = self.reconnect_delays_s[min(self._failure_index, len(self.reconnect_delays_s) - 1)]
        self._failure_index += 1
        return delay

    def reset_after_connect(self) -> None:
        """通道成功工作一次后回到最短退避。"""

        self._failure_index = 0

    def next_probe_delay(self) -> float:
        """下一次探测 WS 的间隔：base ×2 递增，封顶 ``probe_max_s``。"""

        delay = min(self.probe_base_s * (2.0**self._probe_attempt), self.probe_max_s)
        self._probe_attempt += 1
        return delay

    def reset_probe(self) -> None:
        self._probe_attempt = 0

    @property
    def probe_attempts(self) -> int:
        return self._probe_attempt


@dataclass(frozen=True, slots=True)
class CarrierChange:
    """载体切换事件（给服务层做状态上报用）。"""

    previous: str | None
    current: str | None
    reason: str
    at_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous": self.previous,
            "current": self.current,
            "reason": self.reason,
            "at_ms": self.at_ms,
        }


# ------------------------------------------------------ websockets 适配 ---
def _ws_connect_factory() -> tuple[Callable[..., Any], str]:
    """返回 ``(connect, 头参数名)``；兼容 websockets 13/14/15 三种 API。"""

    try:
        from websockets.asyncio.client import connect  # websockets >= 13

        return connect, "additional_headers"
    except ImportError:  # pragma: no cover - 旧版 websockets
        import websockets

        return websockets.connect, "extra_headers"


def _map_ws_error(exc: BaseException) -> LinkError:
    """把 websockets/OS 异常翻译成链路错误（401/410 是终态/鉴权，不做域名轮换）。"""

    try:
        from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
    except ImportError:  # pragma: no cover - websockets 必定被 requirements 固定
        return LinkUnavailable(f"WS 错误: {exc}")

    if isinstance(exc, InvalidStatus):
        status = int(getattr(exc.response, "status_code", 0) or 0)
        if status in (401, 403):
            return LinkAuthError(f"WS 握手鉴权失败 HTTP {status}")
        if status == 410:
            return LinkGoneError("WS 握手被拒：设备已卸载（410）")
        return LinkUnavailable(f"WS 握手失败 HTTP {status}")
    if isinstance(exc, ConnectionClosed):
        code = 0
        received = getattr(exc, "rcvd", None)
        if received is not None:
            code = int(getattr(received, "code", 0) or 0)
        if code == WS_CLOSE_UNAUTHORIZED:
            return LinkAuthError("WS 被服务端以 4401 关闭：令牌失效")
        if code in (WS_CLOSE_GONE, WS_CLOSE_GONE_LEGACY):
            return LinkGoneError(f"WS 被服务端以 {code} 关闭：设备已卸载")
        if code == WS_CLOSE_RATE_LIMITED:
            return LinkRateLimited("WS 被服务端以 4429 关闭：触发限流", retry_after_s=5.0)
        return LinkUnavailable(f"WS 断开 code={code}")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return LinkUnavailable("WS 连接超时")
    if isinstance(exc, OSError):
        return LinkUnavailable(f"WS 网络错误: {exc}")
    if isinstance(exc, WebSocketException):
        return LinkUnavailable(f"WS 协议错误: {exc}")
    return LinkUnavailable(f"WS 错误: {exc}")


def _tls_settings() -> tuple[bool | str, ssl.SSLContext | None]:
    """复用 0.2.10 的两个环境变量，保证现场自签证书机器升级后还能连。

    ``OBS_TLS_INSECURE=1`` 时 HTTP 与 WS 都跳过校验；``OBS_CA_BUNDLE`` 指定
    私有 CA。默认严格校验，不做静默降级。
    """

    if os.environ.get("OBS_TLS_INSECURE", "").strip().lower() in {"1", "true", "yes"}:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return False, context
    bundle = os.environ.get("OBS_CA_BUNDLE", "").strip()
    if bundle:
        try:
            context = ssl.create_default_context(cafile=bundle)
        except (OSError, ssl.SSLError) as exc:
            log.warning("OBS_CA_BUNDLE 无法加载（%s），回退到系统 CA", exc)
            return bundle, None
        return bundle, context
    return True, None


# ------------------------------------------------------------------ Link ---
class Link:
    """唯一链路。所有上行/下行都必须经过它。"""

    def __init__(
        self,
        *,
        token: str = "",
        base_urls: list[str] | None = None,
        device_uid: str = "",
        agent_version: str = "",
        boot_id: str = "",
        clock: Clock | None = None,
        outbox: Outbox | None = None,
        outbox_limits: OutboxLimits | None = None,
        heartbeat_payload: Callable[[], Mapping[str, Any]] | None = None,
        on_carrier_change: Callable[[CarrierChange], None] | None = None,
        on_unauthorized: Callable[[], Awaitable[str | None]] | None = None,
        http_client: httpx.AsyncClient | None = None,
        ws_connector: Any | None = None,
        heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S,
        reconnect_delays_s: tuple[float, ...] | None = None,
        probe_base_s: float = PROBE_BASE_S,
        probe_max_s: float = PROBE_MAX_S,
        http_poll_interval_s: float = MIN_HTTP_ROUND_S,
    ) -> None:
        self._clock = clock or Clock()
        # 注意不能用 ``outbox or Outbox(...)``：Outbox 定义了 __len__，空队列是"假值"，
# 会把调用方注入的持久化 outbox 悄悄换成一份新的内存队列（W1 真机 e2e 踩到）。
        self._outbox = outbox if outbox is not None else Outbox(outbox_limits)
        self._token = str(token or "")
        self._base_urls = [url.rstrip("/") for url in (base_urls or []) if str(url).strip()]
        self._base_index = 0
        self._device_uid = str(device_uid or "")
        self._agent_version = str(agent_version or "")
        self._boot_id = str(boot_id or new_boot_id())
        self._heartbeat_interval_s = self._clamp_heartbeat(heartbeat_interval_s)
        self._heartbeat_payload = heartbeat_payload
        self._on_carrier_change = on_carrier_change
        self._on_unauthorized = on_unauthorized
        self._reconnect_delays_s = tuple(reconnect_delays_s or RECONNECT_DELAYS_S)
        self._probe_base_s = float(probe_base_s)
        self._probe_max_s = float(probe_max_s)
        #: A15 的 ``poll_interval_s``：HTTP 两轮之间的最小间隔（秒）。
        self._http_poll_interval_s = max(MIN_HTTP_ROUND_S, float(http_poll_interval_s))
        #: 测试注入点：``(url, **kwargs) -> awaitable connection``；默认用 websockets。
        self._ws_connector = ws_connector

        verify, ssl_context = _tls_settings()
        self._ssl_context = ssl_context
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(HTTP_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            verify=verify,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

        self._inbox: asyncio.Queue[Envelope | None] = asyncio.Queue(maxsize=INBOX_MAX)
        self._wake = asyncio.Event()
        self._wake_frame = asyncio.Event()
        self._carrier_event = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._closing = False
        self._closed = False
        self._carrier: str | None = None
        self._ws: Any = None
        self._last_error = ""
        self._server_version = ""
        self._unknown_messages = 0
        self._unknown_types: set[str] = set()
        #: 已处理的最后一条**下行**信封 id。HTTP 批次请求的 ``last_ack_id`` 与
        #: WS 的 hello 都带它，服务端据此推进自己的下行 outbox（规格 §5.6）。
        #: 初值从 outbox 的持久化游标恢复，重启后不会让服务端重发已处理的下行。
        self._last_downlink_id = self._outbox.last_downlink_id
        self._terminated = False

    # -- 只读状态 --------------------------------------------------------
    @property
    def carrier(self) -> str | None:
        return self._carrier

    @property
    def connected(self) -> bool:
        return self._carrier is not None

    @property
    def closed(self) -> bool:
        return self._closing

    @property
    def terminated(self) -> bool:
        """设备已被服务端标记卸载：调用方应停止服务并退出。"""

        return self._terminated

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def outbox(self) -> Outbox:
        return self._outbox

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def heartbeat_interval_s(self) -> int:
        return self._heartbeat_interval_s

    @property
    def pending_uplink(self) -> int:
        return self._outbox.pending_count

    @property
    def last_downlink_id(self) -> str:
        """已处理的最后一条下行信封 id（下行游标，与上行 outbox 的 ack 分离）。"""

        return self._last_downlink_id

    def set_token(self, token: str) -> None:
        self._token = str(token or "")

    def set_base_urls(self, urls: list[str]) -> None:
        cleaned = [str(url).rstrip("/") for url in urls if str(url).strip()]
        if cleaned:
            self._base_urls = cleaned
            self._base_index = 0

    def set_heartbeat_interval(self, seconds: int | float) -> int:
        self._heartbeat_interval_s = self._clamp_heartbeat(seconds)
        return self._heartbeat_interval_s

    @staticmethod
    def _clamp_heartbeat(seconds: int | float) -> int:
        try:
            value = int(seconds)
        except (TypeError, ValueError):
            return DEFAULT_HEARTBEAT_INTERVAL_S
        return max(MIN_HEARTBEAT_INTERVAL_S, min(MAX_HEARTBEAT_INTERVAL_S, value))

    def status(self) -> dict[str, Any]:
        return {
            "carrier": self._carrier,
            "connected": self.connected,
            "device_uid": self._device_uid,
            "agent_version": self._agent_version,
            "server_version": self._server_version,
            "heartbeat_interval_s": self._heartbeat_interval_s,
            "token_set": bool(self._token),
            "base_urls": list(self._base_urls),
            "outbox": self._outbox.stats(),
            "last_error": self._last_error,
            "unknown_messages": self._unknown_messages,
            "clock_offset_ms": self._clock.device_time_offset_ms,
        }

    # -- 生命周期 --------------------------------------------------------
    async def start(self) -> None:
        """启动载体/帧上传/心跳三个后台任务；重复调用无副作用。"""

        if self._tasks:
            return
        if self._closing:
            raise LinkClosed("链路已关闭，不能重新启动")
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._carrier_loop(), name="observatory-link-carrier"),
            loop.create_task(self._frame_loop(), name="observatory-link-frames"),
            loop.create_task(self._heartbeat_loop(), name="observatory-link-heartbeat"),
        ]

    async def close(self) -> None:
        """停止全部任务、关闭载体与 HTTP 客户端，并唤醒 ``recv()``。

        可重复调用；即使载体循环因 GONE 终态先退出，这里仍会释放 HTTP 连接等
        资源（``_terminate`` 只停止重连，不做 IO 清理）。
        """

        if self._closed:
            return
        self._closed = True
        self._closing = True
        self._wake.set()
        self._wake_frame.set()
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._close_ws()
        if self._owns_http:
            try:
                await self._http.aclose()
            except Exception as exc:  # noqa: BLE001 - 关闭失败无补救手段，但必须留痕
                log.warning("关闭 HTTP 客户端失败: %s", exc)
        self._set_carrier(None, "链路关闭")
        self._put_sentinel()

    def _put_sentinel(self) -> None:
        """给 ``recv()`` 投递关闭标记；队列满时清一条再放，不阻塞关闭流程。"""

        for _ in range(INBOX_MAX + 1):
            try:
                self._inbox.put_nowait(None)
                return
            except asyncio.QueueFull:
                try:
                    self._inbox.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                    return

    # -- 收 --------------------------------------------------------------
    async def recv(self) -> Envelope:
        """阻塞取下一个下行信封；链路关闭时抛 ``LinkClosed``。

        协议级类型（``ping``/``pong``）在内部消化，不会交给调用方；未知类型
        记日志后丢弃（"忽略且可观测"）。
        """

        item = await self._inbox.get()
        if item is None:
            raise LinkClosed("链路已关闭")
        return item

    async def wait_carrier_change(self, timeout: float | None = None) -> str | None:
        """等待一次载体切换事件；返回切换后的载体名，超时返回 ``None``。"""

        self._carrier_event.clear()
        try:
            await asyncio.wait_for(self._carrier_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._carrier

    # -- 发 --------------------------------------------------------------
    async def send(
        self,
        kind: str,
        body: Mapping[str, Any] | None = None,
        *,
        ts_ms: int | None = None,
        msg_id: str | None = None,
    ) -> str:
        """把一条上行信封放进 outbox 并唤醒载体；返回信封 id（幂等键）。

        上行时间字段统一在这里补 ``device_time_ms``（C5），其余字段由调用方给。
        入队失败（outbox 满且策略为 reject）会抛 ``OutboxFull``：调用方必须记
        日志/上报，不允许悄悄吞掉。
        """

        if self._closing:
            raise LinkClosed("链路已关闭")
        now_ms = int(ts_ms) if ts_ms is not None else self._clock.device_now_ms()
        payload = dict(body or {})
        payload.setdefault("device_time_ms", now_ms)
        envelope = self._outbox.append_envelope(kind, payload, ts_ms=now_ms, msg_id=msg_id)
        self._wake.set()
        return envelope.id

    async def send_control_frame(
        self,
        session: str,
        payload: bytes,
        *,
        ts_ms: int | None = None,
    ) -> bool:
        """实时控制帧（二进制，仅 WS 载体）；HTTP 载体返回 ``False``。

        控制流是载体能力而不是协议语义：服务端在设备只有 HTTP 载体时不会下发
        ``control``，所以这里只记 debug 并拒绝，不排队、不改变链路状态。
        """

        ws = self._ws
        if ws is None or self._carrier != "ws":
            log.debug("当前载体 %s 不支持二进制控制帧，丢弃 session=%s", self._carrier, session)
            return False
        now_ms = int(ts_ms) if ts_ms is not None else self._clock.device_now_ms()
        blob = encode_binary({"t": "ctrl", "session": str(session), "ts_ms": now_ms}, payload)
        try:
            await ws.send(blob)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 帧丢失只影响实时画面，链路会自愈
            log.warning("控制帧发送失败 session=%s: %s", session, exc)
            return False

    def enqueue_frame(
        self,
        payload: bytes,
        *,
        device_ts_ms: int | None = None,
        session_id: int = 0,
        kind: str = "screen",
        frame_id: str | None = None,
        width: int = 0,
        height: int = 0,
        score: int = 0,
    ) -> str:
        """入队一帧（唯一的帧上传路径，C2）；返回 ``X-Frame-Id``。

        帧与链路载体无关：无论当前是 WS 还是 HTTP，帧都从 outbox 走
        ``POST /api/v1/agent/frames``。``device_ts_ms`` 是设备采集时刻（服务端
        另有接收时间 ``ts_ms``，见规格 §6bis）。
        """

        if self._closing:
            raise LinkClosed("链路已关闭")
        captured_ms = int(device_ts_ms) if device_ts_ms is not None else self._clock.device_now_ms()
        identifier = self._outbox.append_frame(
            payload,
            ts_ms=captured_ms,
            session_id=int(session_id),
            kind=str(kind),
            frame_id=frame_id,
            width=int(width),
            height=int(height),
            score=int(score),
        )
        self._wake_frame.set()
        return identifier

    # ---------------------------------------------------------- 更新包下载 ---
    async def download_package(self, version: str, destination: Path) -> Path:
        """下载指定版本的受控端安装包到 ``destination``（更新链路唯一的下载实现）。

        只负责**传输**：sha256 校验由 ``tasks/update.stage_update`` 在下载完成后做
        （A3 的唯一校验点），这里不重复实现一套校验逻辑。
        """

        if not version:
            raise LinkBadRequest("下载安装包缺少版本号")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        response = await self._request(
            "GET", f"/api/v1/public/agent/{version}/package", timeout=300.0
        )
        self._raise_for_status(response, what="package")
        digest = hashlib.sha256()
        written = 0
        try:
            with open(temporary, "wb") as handle:
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise LinkUnavailable(f"写入安装包失败: {exc}") from exc
        except asyncio.CancelledError:
            temporary.unlink(missing_ok=True)
            raise
        if written <= 0:
            temporary.unlink(missing_ok=True)
            raise LinkUnavailable(f"安装包为空（version={version}）")
        os.replace(temporary, target)
        log.info("下载安装包完成 version=%s bytes=%s sha256=%s", version, written, digest.hexdigest()[:12])
        return target

    # -- 注册 ------------------------------------------------------------
    async def enroll(
        self,
        *,
        code: str,
        install_token: str = "",
        device_uid: str,
        hostname: str,
        os_name: str,
        os_version: str,
        arch: str,
        agent_version: str,
        sysinfo: Mapping[str, Any] | None,
        sessions: list[Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        """入网注册（0.2.x 与 0.3.0 共用的引导端点）。

        注册是"链路上线之前"的一次性动作，不是第二条心跳路径：拿到令牌后所有
        上行一律走 ``send``/``enqueue_frame``。

        ``install_token``（A18，一次性令牌）与 ``code``（旧服务端共享注册码）
        **同时发送**：新服务端优先校验安装令牌，旧服务端只认 ``code``，这样同一份
        受控端二进制在 0.2.x 与 0.3.0 服务端上都能入网（N2 零重装升级）。
        """

        body = {
            "code": str(code),
            "install_token": str(install_token or ""),
            "uid": str(device_uid),
            "hostname": str(hostname),
            "os_name": str(os_name),
            "os_version": str(os_version),
            "arch": str(arch),
            "agent_version": str(agent_version),
            "sysinfo": dict(sysinfo or {}),
            "sessions": [dict(item) for item in (sessions or [])],
        }
        response = await self._request("POST", ENROLL_PATH, json=body, timeout=ENROLL_TIMEOUT_S)
        self._raise_for_status(response, what="enroll")
        try:
            data = response.json()
        except ValueError as exc:
            raise LinkUnavailable(f"注册响应不是 JSON: {exc}") from exc
        if not isinstance(data, Mapping):
            raise LinkUnavailable("注册响应不是对象")
        token = data.get("token")
        if isinstance(token, str) and token:
            self._token = token
        base_urls = data.get("base_urls")
        if isinstance(base_urls, list) and base_urls:
            self.set_base_urls([str(url) for url in base_urls])
        server_version = data.get("server_version")
        if isinstance(server_version, str):
            self._server_version = server_version
        return dict(data)

    # --------------------------------------------------------- 载体循环 ---
    async def _carrier_loop(self) -> None:
        policy = CarrierPolicy(
            reconnect_delays_s=self._reconnect_delays_s,
            probe_base_s=self._probe_base_s,
            probe_max_s=self._probe_max_s,
        )
        try:
            while not self._closing:
                # 1) 首选 WS。连不上/断开都直接落到 HTTP（"ws 失败→http"）。
                try:
                    await self._run_ws(policy)
                except LinkAuthError as exc:
                    self._last_error = str(exc)
                    log.warning("WS 鉴权失败：%s", exc)
                    await self._reauth_or_wait()
                    continue
                except LinkGoneError as exc:
                    await self._terminate(str(exc))
                    return
                except LinkRateLimited as exc:
                    self._last_error = f"ws: {exc}"
                    delay = min(max(exc.retry_after_s, policy.next_reconnect_delay()), 120.0)
                    log.warning("WS 被限流（%s），%.0fs 后再试", exc, delay)
                    await asyncio.sleep(delay)
                    continue
                except LinkUnavailable as exc:
                    self._last_error = f"ws: {exc}"
                    log.warning("WS 载体不可用（%s），降级到 HTTP 长轮询", exc)
                except LinkError as exc:
                    self._last_error = f"ws: {exc}"
                    log.warning("WS 载体错误（%s），降级到 HTTP 长轮询", exc)
                if self._closing:
                    return

                # 2) HTTP 长轮询；返回值表示是否探测成功、可以切回 WS。
                try:
                    upgraded = await self._run_http_phase(policy)
                except LinkAuthError as exc:
                    self._last_error = str(exc)
                    log.warning("HTTP 鉴权失败：%s", exc)
                    await self._reauth_or_wait()
                    continue
                except LinkGoneError as exc:
                    await self._terminate(str(exc))
                    return
                if self._closing:
                    return
                if upgraded:
                    log.info("WS 探测成功，切回 WebSocket 载体")
                    continue
        except asyncio.CancelledError:
            raise
        finally:
            self._set_carrier(None, "载体循环退出")

    async def _run_ws(self, policy: CarrierPolicy) -> None:
        """跑一轮 WS 会话：连接 → hello → 收发，直到断开或出错。

        正常返回表示"连上过但被对端关闭"；异常表示连不上/鉴权失败/被卸载。
        调用方统一降级到 HTTP。
        """

        ws = await self._open_ws()
        policy.reset_after_connect()
        self._ws = ws
        self._set_carrier("ws", "websocket 已连接")
        try:
            await self._send_hello()
            while not self._closing:
                # 先清唤醒标记再 flush：这样 flush 之后新入队的消息会让下一轮
                # 等待立刻返回，不会因为"清标记晚于入队"丢掉唤醒。
                self._wake.clear()
                await self._flush_ws(ws)
                await self._ws_receive_once(ws)
        finally:
            self._ws = None
            await self._close_ws(ws)
            if self._carrier == "ws":
                self._set_carrier(None, "websocket 断开")
            # 未写成功/未确认的条目重投；已确认的不会被重放。
            self._outbox.replay()

    async def _ws_receive_once(self, ws: Any) -> None:
        """等一条下行/唤醒；两种来源谁先到都行。"""

        recv_task = asyncio.create_task(ws.recv())
        wake_task = asyncio.create_task(self._wake.wait())
        try:
            done, _pending = await asyncio.wait(
                {recv_task, wake_task},
                timeout=max(30.0, self._heartbeat_interval_s * 3.0),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            recv_task.cancel()
            wake_task.cancel()
            await asyncio.gather(recv_task, wake_task, return_exceptions=True)
            raise
        if recv_task in done:
            wake_task.cancel()
            try:
                raw = recv_task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一翻译成链路错误
                raise _map_ws_error(exc) from exc
            finally:
                await asyncio.gather(wake_task, return_exceptions=True)
            if isinstance(raw, (bytes, bytearray)):
                self._on_binary(bytes(raw))
            elif isinstance(raw, str):
                try:
                    envelope = decode(raw)
                except EnvelopeError as exc:
                    log.warning("收到无法解析的下行信封（忽略）: %s", exc)
                    return
                await self._on_downlink(envelope)
            else:
                log.warning("收到未知 WS 消息类型 %s（忽略）", type(raw).__name__)
            return
        # 只等到唤醒（或空闲超时）：清掉标记后回到循环顶部 flush。
        recv_task.cancel()
        await asyncio.gather(recv_task, wake_task, return_exceptions=True)
        self._wake.clear()

    async def _flush_ws(self, ws: Any) -> None:
        """把 outbox 里未发送的信封写进 WS，并标记"已交给载体"。

        注意：**不在写成功后删除**。规格 §5.6 的确认游标是服务端返回的
        ``ack_id``（WS 载体上随 pong/回执信封下发），只有它能推进并清空 outbox。
        这样断线时未确认的消息会在重连后重投，服务端按信封 id 去重——真正的
        至少一次。``mark_sent`` 只避免同一连接内重复写。
        """

        entries = self._outbox.unsent_envelopes(limit=BATCH_MAX_ITEMS)
        for entry in entries:
            try:
                # 信封走 **文本帧**（JSON），WS 的二进制帧只用于实时控制画面
                # （规格 §5.1）；把 JSON 编成 bytes 发出去会让服务端当成控制帧。
                await ws.send(entry.to_envelope().to_json())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 交给载体循环降级/重连
                raise _map_ws_error(exc) from exc
            self._outbox.mark_sent((entry.msg_id,), now_ms=self._clock.device_now_ms())

    async def _send_hello(self) -> None:
        """首条上行 hello（规格 §5.6 的字段集合，逐字段一致）。"""

        await self.send(
            "hello",
            {
                "agent_version": self._agent_version,
                "protocol": 3,
                "boot_id": self._boot_id,
                "device_uid": self._device_uid,
                "last_ack_id": self._last_downlink_id,
                "device_time_ms": self._clock.device_now_ms(),
            },
            ts_ms=self._clock.device_now_ms(),
        )

    async def _open_ws(self) -> Any:
        """连接 WS 载体；按线路列表轮换（连接层失败才轮换，401/410 不轮换）。"""

        if not self._base_urls:
            raise LinkUnavailable("没有配置服务器线路，无法连接 WS")
        if self._ws_connector is not None:
            factory, header_key = self._ws_connector, "additional_headers"
        else:
            factory, header_key = _ws_connect_factory()
        kwargs: dict[str, Any] = {
            "open_timeout": WS_OPEN_TIMEOUT_S,
            "max_size": WS_MAX_MESSAGE_BYTES,
            "ping_interval": max(10, self._heartbeat_interval_s),
            "ping_timeout": max(10, self._heartbeat_interval_s),
            header_key: {DEVICE_TOKEN_HEADER: self._token},
        }
        if self._ssl_context is not None:
            kwargs["ssl"] = self._ssl_context
        last_error: BaseException | None = None
        for _ in range(max(1, len(self._base_urls))):
            url = self._ws_url(self._base_urls[self._base_index])
            try:
                log.info("连接 WebSocket 载体: %s", url)
                return await factory(url, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 先翻译，再决定是否轮换
                mapped = _map_ws_error(exc)
                if not mapped.retryable:
                    raise mapped from exc
                last_error = exc
                log.warning("WS 线路 %s 不可用: %s", url, mapped)
                self._rotate_base()
        raise LinkUnavailable(f"WS 全部线路不可达: {last_error}")

    def _ws_url(self, base: str) -> str:
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :] + WS_PATH
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :] + WS_PATH
        return base + WS_PATH

    async def _close_ws(self, ws: Any | None = None) -> None:
        target = ws if ws is not None else self._ws
        if ws is None:
            self._ws = None
        if target is None:
            return
        try:
            await target.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 关闭失败只能记录
            log.debug("关闭 WS 失败: %s", exc)

    # ------------------------------------------------------- HTTP 载体 ---
    async def _run_http_phase(self, policy: CarrierPolicy) -> bool:
        """跑 HTTP 长轮询阶段；返回 ``True`` 表示探测到 WS 可用、应切回。

        两条时间线在这里并存：
        - **上行唤醒**：``send()`` set ``_wake``，放弃在途请求（id 幂等，下一轮重投）
          立刻带上新消息重发，回执延迟是"下一次调度"而不是最长一个 hold；
        - **向上探测**：``transport_reprobe_s`` 到点就放弃在途长轮询去探测 WS。
          服务端的 hold 最长 20s，不能让它把"切回 WS"拖到几十秒之后（W1 e2e 实测
          过这个延迟，A15 的探测间隔必须是独立定时器）。
        """

        self._set_carrier("http", "http 长轮询")
        policy.reset_probe()
        next_probe_at: float | None = None
        loop = asyncio.get_running_loop()
        interval = max(MIN_HTTP_ROUND_S, self._http_poll_interval_s)
        while not self._closing:
            started = loop.time()
            self._wake.clear()
            if next_probe_at is None:
                probe_wait = min(policy.next_probe_delay(), self._probe_max_s)
                next_probe_at = started + probe_wait
                log.info("HTTP 载体稳定，%.0fs 后探测 WS", probe_wait)
            probe_due = max(0.0, next_probe_at - loop.time())
            round_task = asyncio.create_task(self._http_round(), name="link-http-round")
            wake_task = asyncio.create_task(self._wake.wait(), name="link-http-wake")
            try:
                done, _pending = await asyncio.wait(
                    {round_task, wake_task}, timeout=probe_due, return_when=asyncio.FIRST_COMPLETED
                )
                if round_task not in done:
                    # 要么有新上行要立刻发，要么到了向上探测时间：都值得放弃在途
                    # 长轮询请求（幂等）。
                    round_task.cancel()
                    wake_task.cancel()
                    await asyncio.gather(round_task, wake_task, return_exceptions=True)
                    if self._wake.is_set():
                        self._wake.clear()
                        continue
                    if await self._probe_ws():
                        return True
                    probe_wait = min(policy.next_probe_delay(), self._probe_max_s)
                    next_probe_at = loop.time() + probe_wait
                    log.info("WS 仍不可用，%.0fs 后再探测", probe_wait)
                    continue
                wake_task.cancel()
                await asyncio.gather(wake_task, return_exceptions=True)
                round_task.result()
            except LinkRateLimited as exc:
                self._last_error = str(exc)
                log.warning("HTTP 载体被限流，%.0fs 后重试", exc.retry_after_s)
                await asyncio.sleep(exc.retry_after_s)
                continue
            except LinkBadRequest as exc:
                self._last_error = str(exc)
                # 批次被服务端判为非法：丢弃这一批上行（已记错误日志），否则会
                # 无限重试同一批毒消息，把链路拖死。
                for msg_id in exc.msg_ids:
                    self._outbox.discard(msg_id, reason=f"http_bad_request: {exc}")
                log.error("HTTP 批次被拒绝（已丢弃该批上行）: %s", exc)
                await asyncio.sleep(1.0)
                continue
            except LinkAuthError:
                raise
            except LinkGoneError:
                raise
            except LinkError as exc:
                self._last_error = str(exc)
                delay = policy.next_reconnect_delay()
                log.warning("HTTP 载体失败（%s），%.0fs 后重试", exc, delay)
                await asyncio.sleep(delay)
                continue
            except asyncio.CancelledError:
                raise

            policy.reset_after_connect()
            if loop.time() >= next_probe_at:
                if await self._probe_ws():
                    return True
                probe_wait = min(policy.next_probe_delay(), self._probe_max_s)
                next_probe_at = loop.time() + probe_wait
                log.info("WS 仍不可用，%.0fs 后再探测", probe_wait)
            elapsed = loop.time() - started
            if elapsed < interval:
                await asyncio.sleep(min(interval - elapsed, max(0.05, next_probe_at - loop.time())))
        return False

    async def _http_round(self) -> None:
        """一次上行批次 → 下行批次的往返；成功时推进 outbox 与时钟。"""

        entries = self._outbox.pending(limit=BATCH_MAX_ITEMS, frames=False)
        sent_ids = tuple(entry.msg_id for entry in entries)
        # 请求里的 last_ack_id 是**下行**游标（已处理的最后一条下行 id），
        # 让服务端推进它自己的下行 outbox；与上行确认彻底分开（规格 §5.6）。
        batch = build_uplink_batch(
            [entry.to_envelope() for entry in entries], last_ack_id=self._last_downlink_id
        )
        started_ms = self._clock.device_now_ms()
        timeout = max(HTTP_TIMEOUT_S, self._http_poll_interval_s + 10.0)
        response = await self._request("POST", LINK_PATH, json=batch, timeout=timeout)
        finished_ms = self._clock.device_now_ms()
        self._raise_for_status(response, msg_ids=sent_ids, what="link")
        try:
            data = response.json()
        except ValueError as exc:
            raise LinkUnavailable(f"下行批次不是 JSON: {exc}") from exc
        if not isinstance(data, Mapping):
            raise LinkUnavailable("下行批次不是对象")
        try:
            downlink = parse_downlink_batch(data)
        except EnvelopeError as exc:
            raise LinkUnavailable(f"下行批次解析失败: {exc}") from exc

        # 200 表示这一批已被服务端接收。优先按服务端返回的上行确认游标 ack_id
# 前移（规格 §5.6）；若服务端没给 ack_id，则以"本批已接收"为确认依据。
        if downlink.ack_id:
            self._outbox.ack_through(downlink.ack_id)
        else:
            for msg_id in sent_ids:
                self._outbox.ack(msg_id)
        if downlink.server_time_ms > 0:
            try:
                self._clock.observe(
                    device_time_ms=finished_ms,
                    server_time_ms=downlink.server_time_ms,
                    rtt_ms=max(0, finished_ms - started_ms),
                )
            except ValueError as exc:
                log.warning("服务端时间采样非法（忽略本次校准）: %s", exc)
        for envelope in downlink.items:
            await self._on_downlink(envelope)
        if self._wake.is_set():
            self._wake.clear()

    async def _probe_ws(self) -> bool:
        """探测 WS 是否恢复；连上即关（真正的会话交给载体循环重建）。"""

        try:
            ws = await self._open_ws()
        except LinkGoneError:
            raise
        except LinkAuthError as exc:
            log.warning("WS 探测鉴权失败：%s", exc)
            await self._reauth_or_wait()
            return False
        except LinkError as exc:
            log.debug("WS 探测失败: %s", exc)
            return False
        try:
            await ws.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 探测连接关闭失败无影响
            log.debug("关闭探测连接失败: %s", exc)
        return True

    # --------------------------------------------------------- 帧上传 ---
    async def _frame_loop(self) -> None:
        """唯一的帧上传循环：从 outbox 取帧 → ``POST /agent/frames`` → 确认。"""

        delays = FRAME_RETRY_DELAYS_S
        failure_index = 0
        try:
            while not self._closing:
                entry = self._outbox.next_frame()
                if entry is None:
                    self._wake_frame.clear()
                    try:
                        await asyncio.wait_for(self._wake_frame.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        log.debug("帧队列空闲等待超时（正常）")
                    continue
                try:
                    await self._upload_frame(entry)
                except LinkAuthError as exc:
                    self._last_error = str(exc)
                    log.warning("帧上传鉴权失败：%s", exc)
                    await self._reauth_or_wait()
                    continue
                except LinkGoneError:
                    raise
                except LinkRateLimited as exc:
                    self._last_error = str(exc)
                    log.warning("帧上传被限流，%.0fs 后重试", exc.retry_after_s)
                    await asyncio.sleep(exc.retry_after_s)
                    continue
                except LinkBadRequest as exc:
                    # 服务端明确拒绝这一帧（例如格式/摘要错误）：丢弃并记错误，
                    # 否则同一条毒帧会永远堵在队头（C6：可观测失败）。
                    self._outbox.discard(entry.msg_id, reason=f"frame_bad_request: {exc}")
                    log.error("帧 %s 被服务端拒绝，已丢弃: %s", entry.msg_id, exc)
                    continue
                except LinkError as exc:
                    self._last_error = str(exc)
                    delay = delays[min(failure_index, len(delays) - 1)]
                    failure_index += 1
                    log.warning(
                        "帧上传失败（%s），%.0fs 后重试（待传 %s 帧）",
                        exc,
                        delay,
                        self._outbox.stats()["frames"],
                    )
                    await asyncio.sleep(delay)
                    continue
                failure_index = 0
        except LinkGoneError as exc:
            await self._terminate(str(exc))
        except asyncio.CancelledError:
            raise
        finally:
            log.debug("帧上传循环退出")

    async def _upload_frame(self, entry: OutboxEntry) -> None:
        if entry.payload is None:  # pragma: no cover - 不变量：帧条目必有载荷
            raise LinkBadRequest("帧条目缺少载荷", msg_ids=(entry.msg_id,))
        params = {
            # 规格 §6bis：设备采集时刻叫 device_ts_ms；服务端另记接收时间 ts_ms。
            "device_ts_ms": int(entry.ts_ms),
            "session_id": int(entry.session_id),
            "kind": str(entry.body.get("kind") or "screen"),
        }
        headers = {"X-Frame-Id": entry.msg_id, "Content-Type": "image/jpeg"}
        response = await self._request(
            "POST",
            FRAME_PATH,
            content=entry.payload,
            params=params,
            headers=headers,
            timeout=FRAME_TIMEOUT_S,
        )
        self._raise_for_status(response, msg_ids=(entry.msg_id,), what="frames")
        self._outbox.frame_done(entry.msg_id)

    # --------------------------------------------------------- 心跳 ---
    async def _heartbeat_loop(self) -> None:
        """心跳就是一条普通上行信封（不是独立 HTTP 端点循环）。"""

        try:
            while not self._closing:
                await asyncio.sleep(self._heartbeat_interval_s)
                if self._closing:
                    return
                body: dict[str, Any] = {
                    "agent_version": self._agent_version,
                    "boot_id": self._boot_id,
                    "device_time_ms": self._clock.device_now_ms(),
                }
                if self._heartbeat_payload is not None:
                    try:
                        extra = self._heartbeat_payload()
                    except Exception:  # noqa: BLE001 - 载荷故障降级为最小心跳
                        log.exception("心跳载荷生成失败，发送最小心跳")
                        extra = {}
                    if isinstance(extra, Mapping):
                        body.update(extra)
                try:
                    await self.send("heartbeat", body)
                except OutboxFull as exc:
                    log.error("心跳入队失败（outbox 满）: %s", exc)
                except LinkClosed:
                    return
        except asyncio.CancelledError:
            raise

    # --------------------------------------------------- 下行分发 ---
    async def _on_downlink(self, envelope: Envelope) -> None:
        """处理一条下行信封：协议级类型内部消化，其余交给 ``recv()``。

        每一条成功解码并"处理"的下行都会推进下行游标 ``_last_downlink_id``，
        下次 HTTP 请求/WS hello 用它告诉服务端"这些下行我已经收到"。
        """

        previous_downlink = self._last_downlink_id
        self._last_downlink_id = envelope.id
        self._outbox.note_downlink(envelope.id)

        if not is_known_type(envelope.type):
            self._unknown_messages += 1
            first_time = envelope.type not in self._unknown_types
            if first_time and len(self._unknown_types) < UNKNOWN_TYPE_LOG_LIMIT:
                self._unknown_types.add(envelope.type)
                log.warning("收到未知下行信封类型 %r（id=%s），已忽略", envelope.type, envelope.id)
            elif not first_time:
                log.debug("再次收到未知下行类型 %r，忽略", envelope.type)
            return

        body = envelope.body_dict
        server_time_ms = body.get("server_time_ms")
        if isinstance(server_time_ms, int) and not isinstance(server_time_ms, bool) and server_time_ms > 0:
            try:
                self._clock.observe(
                    device_time_ms=self._clock.device_now_ms(), server_time_ms=server_time_ms
                )
            except ValueError as exc:
                log.warning("下行信封 server_time_ms 非法（忽略）: %s", exc)
        # 服务端的上行确认游标：推进 outbox（与下行游标互不混用，规格 §5.6）。
        ack_id = body.get("ack_id")
        if isinstance(ack_id, str) and ack_id:
            self._outbox.ack_through(ack_id)
        elif previous_downlink and body.get("last_ack_id") == previous_downlink:
            log.debug("服务端回显下行游标 %s（无需处理）", previous_downlink)

        if envelope.type == "ping":
            await self._reply_pong(envelope)
            return
        if envelope.type == "pong":
            # 心跳回执：只用于 ack_id/时钟校准，不进业务队列。
            return
        if envelope.type == "welcome":
            self._apply_welcome(body)
        elif envelope.type == "config":
            self._apply_config(body)
        await self._deliver(envelope)

    async def _reply_pong(self, _ping: Envelope) -> None:
        try:
            await self.send("pong", {"device_time_ms": self._clock.device_now_ms()})
        except OutboxFull as exc:
            log.error("pong 入队失败（outbox 满）: %s", exc)
        except LinkClosed:
            return

    def _apply_welcome(self, body: Mapping[str, Any]) -> None:
        interval = body.get("heartbeat_interval_s")
        if isinstance(interval, int) and not isinstance(interval, bool):
            self._heartbeat_interval_s = self._clamp_heartbeat(interval)
        server_version = body.get("server_version")
        if isinstance(server_version, str) and server_version:
            self._server_version = server_version
        log.info(
            "链路已建立 welcome: server=%s heartbeat=%ss carrier=%s",
            self._server_version or "?",
            self._heartbeat_interval_s,
            self._carrier,
        )

    def _apply_config(self, body: Mapping[str, Any]) -> None:
        interval = body.get("heartbeat_interval_s")
        if interval is None:
            config = body.get("config")
            if isinstance(config, Mapping):
                interval = config.get("heartbeat_interval_s")
        if isinstance(interval, int) and not isinstance(interval, bool):
            clamped = self._clamp_heartbeat(interval)
            if clamped != self._heartbeat_interval_s:
                log.info("心跳间隔更新为 %ss", clamped)
            self._heartbeat_interval_s = clamped

    async def _deliver(self, envelope: Envelope) -> None:
        try:
            await asyncio.wait_for(self._inbox.put(envelope), timeout=INBOX_PUT_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.error(
                "下行队列已满 %ss，丢弃信封 %s id=%s（服务端会因回执超时重投）",
                INBOX_PUT_TIMEOUT_S,
                envelope.type,
                envelope.id,
            )

    def _on_binary(self, blob: bytes) -> None:
        """WS 二进制下行：规格只定义上行控制帧；下行的未知二进制记日志忽略。"""

        try:
            header, payload = decode_binary(blob)
        except EnvelopeError as exc:
            log.warning("收到无法解析的 WS 二进制帧（忽略）: %s", exc)
            return
        log.warning(
            "收到未预期的 WS 二进制帧 t=%r payload=%s 字节（忽略）",
            header.get("t"),
            len(payload),
        )

    # --------------------------------------------------- 载体切换 ---
    def _set_carrier(self, carrier: str | None, reason: str) -> None:
        previous = self._carrier
        if previous == carrier:
            return
        self._carrier = carrier
        change = CarrierChange(
            previous=previous,
            current=carrier,
            reason=reason,
            at_ms=self._clock.device_now_ms(),
        )
        if carrier is None:
            log.info("链路载体下线（%s）", reason)
        else:
            log.info("链路载体切换：%s -> %s（%s）", previous or "none", carrier, reason)
        self._carrier_event.set()
        callback = self._on_carrier_change
        if callback is not None:
            try:
                callback(change)
            except Exception:  # noqa: BLE001 - 观察者故障不能影响链路
                log.exception("载体切换回调抛异常")

    async def _terminate(self, reason: str) -> None:
        self._last_error = reason
        self._terminated = True
        log.error("链路进入终态：%s（设备已被服务端标记卸载，不再重连）", reason)
        self._set_carrier(None, reason)
        self._closing = True
        self._put_sentinel()

    async def _reauth_or_wait(self) -> None:
        """401 之后走一次重新注册（A17）；失败则等待后重试，不做静默重连。"""

        callback = self._on_unauthorized
        if callback is None:
            log.error("设备令牌被拒绝，但没有配置重新注册回调；%.0fs 后重试", REAUTH_RETRY_S)
            self._outbox.replay()
            await asyncio.sleep(REAUTH_RETRY_S)
            return
        log.warning("设备令牌被拒（401），执行重新注册（A17）")
        try:
            token = await callback()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 注册失败要可观测，然后退避重试
            log.exception("重新注册失败")
            token = None
        if token:
            self._token = str(token)
            log.info("重新注册成功，链路将继续使用新令牌")
            self._outbox.replay()
            return
        log.error("重新注册没有拿到新令牌；%.0fs 后重试", REAUTH_RETRY_S)
        self._outbox.replay()
        await asyncio.sleep(REAUTH_RETRY_S)

    # --------------------------------------------------- HTTP 细节 ---
    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """带线路轮换的 HTTP 请求（只对连接层失败轮换，不对状态码轮换）。"""

        if not self._base_urls:
            raise LinkUnavailable("没有配置服务器线路")
        extra_headers = kwargs.pop("headers", None)
        headers = self._headers(extra_headers)
        last_error: BaseException | None = None
        for _ in range(max(1, len(self._base_urls))):
            url = f"{self._base_urls[self._base_index]}{path}"
            try:
                return await self._http.request(method, url, headers=headers, **kwargs)
            except asyncio.CancelledError:
                raise
            except (httpx.TransportError, httpx.TimeoutException, OSError) as exc:
                last_error = exc
                log.warning("%s %s 线路 %s 失败: %s", method, path, self._base_urls[self._base_index], exc)
                self._rotate_base()
        raise LinkUnavailable(f"{method} {path} 全部线路不可达: {last_error}")

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"ObservatoryAgent/{self._agent_version or '0'}",
        }
        if self._token:
            headers[DEVICE_TOKEN_HEADER] = self._token
        if extra:
            headers.update({str(key): str(value) for key, value in extra.items()})
        return headers

    def _rotate_base(self) -> None:
        if len(self._base_urls) > 1:
            self._base_index = (self._base_index + 1) % len(self._base_urls)

    def _raise_for_status(
        self,
        response: httpx.Response,
        *,
        msg_ids: tuple[str, ...] = (),
        what: str,
    ) -> None:
        """HTTP 状态码 → 链路错误（规格 §5.5 的映射）。"""

        status = response.status_code
        if 200 <= status < 300:
            return
        detail = ""
        try:
            detail = response.text[:200]
        except Exception as exc:  # noqa: BLE001 - 读不到响应体不影响错误分类
            log.debug("读取错误响应体失败: %s", exc)
        if status in (401, 403):
            raise LinkAuthError(f"{what} 鉴权失败 HTTP {status}: {detail}")
        if status == 410:
            raise LinkGoneError(f"{what} 设备已卸载 HTTP 410: {detail}")
        if status == 422:
            raise LinkBadRequest(f"{what} 请求非法 HTTP 422: {detail}", msg_ids=msg_ids)
        if status == 429:
            raise LinkRateLimited(f"{what} 被限流 HTTP 429: {detail}", retry_after_s=_retry_after_s(response))
        if status >= 500:
            raise LinkUnavailable(f"{what} 服务端错误 HTTP {status}: {detail}")
        raise LinkBadRequest(f"{what} 请求失败 HTTP {status}: {detail}", msg_ids=msg_ids)


def _retry_after_s(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return 5.0
    try:
        return max(0.0, min(60.0, float(raw)))
    except ValueError:
        log.debug("忽略无法解析的 Retry-After=%r", raw)
        return 5.0


__all__ = [
    "CONNECT_TIMEOUT_S",
    "CarrierChange",
    "CarrierPolicy",
    "DEVICE_TOKEN_HEADER",
    "ENROLL_PATH",
    "FRAME_PATH",
    "FRAME_RETRY_DELAYS_S",
    "HTTP_HOLD_S",
    "HTTP_TIMEOUT_S",
    "LINK_PATH",
    "Link",
    "LinkAuthError",
    "LinkBadRequest",
    "LinkClosed",
    "LinkError",
    "LinkGoneError",
    "LinkRateLimited",
    "LinkUnavailable",
    "MIN_HTTP_ROUND_S",
    "PROBE_BASE_S",
    "PROBE_MAX_S",
    "RECONNECT_DELAYS_S",
    "WS_CLOSE_GONE",
    "WS_CLOSE_GONE_LEGACY",
    "WS_CLOSE_RATE_LIMITED",
    "WS_CLOSE_UNAUTHORIZED",
    "WS_PATH",
    "probe_delay",
    "probe_delays",
    "reconnect_delays",
]