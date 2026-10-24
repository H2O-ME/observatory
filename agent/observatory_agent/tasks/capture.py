"""单一采集引擎（``tasks`` 包）。

0.2.10 的采集逻辑有两套：``worker._capture_loop``（周期采集 + 变化判定）和
``worker._control_loop``（实时控制流），而 ``runner._capture_health`` 又在服务
进程里用另一套规则推断"能不能采屏"。同一件事三处实现，正是"修一个 bug 多一个
bug"的来源。0.3.0 只保留这一个 ``CaptureEngine``：

- 周期采集、变化判定、自适应退避（5s/20s/60s；强制帧 60s/240s/300s）；
- 实时控制帧（同一套 grab + 编码，只是不参与周期策略）；
- 采集健康（``health()``）由引擎自己产出，服务端只负责转发，不再二次推断；
- 编码统一为 JPEG（规格 N4：受控端 q80 中间图，服务端再转 WebP q75）。

可测性：变化判定、间隔阶梯、``CapturePolicy`` 状态机都是纯函数/纯状态机，
时间一律作为参数传入；``grab`` 通过 ``FrameSource`` 协议注入，单测不需要真实
桌面（给假 source 返回 PIL 图像即可）。

线程模型：一个进程内一个引擎、一个采集线程。mss 的 DXGI/GDI 上下文不是线程
安全的，``MssSource`` 因此按线程持有实例；``CapturePolicy`` 的状态被采集/控制/
心跳/主循环多个线程读写，0.3.1（F36）起用 ``RLock`` 串行（``_streak += 1`` 是
读改写，无锁会丢更新）。
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from PIL import Image

log = logging.getLogger(__name__)

#: 变化指纹尺寸：64×36 灰度网格（比 dHash 便宜，静止桌面也能稳定判定）。
SIGNATURE_WIDTH: Final[int] = 64
SIGNATURE_HEIGHT: Final[int] = 36
#: 没有上一帧指纹时的"分数"：视为必然变化。
SIGNATURE_CHANGED: Final[int] = 255

#: 静止画面的检查间隔阶梯（秒）：有变化立刻回落到 5s。
CHANGE_INTERVALS_S: Final[tuple[int, ...]] = (5, 20, 60)
#: 静止画面下强制出帧的间隔阶梯（秒）：保证时间线上不会出现大片空白。
FORCED_INTERVALS_S: Final[tuple[int, ...]] = (60, 240, 300)
#: 0.3.1（F36）：连续采集失败时的重试退避阶梯（毫秒）：0.5s → 2s → 10s 封顶。
#: 没有它，锁屏一夜的机器会以 500ms 节奏空转 grab ~7 万次。
FAILURE_BACKOFF_MS: Final[tuple[int, ...]] = (500, 2_000, 10_000)

#: 0.3.1（F36）：变化判定的 top-k 格数——全桌平均差对局部变化（小弹窗）不敏感，
#: 取绝对差最大的 32 格平均，一个 ~0.26 分的小弹窗就能被检出。
SIGNATURE_TOP_K: Final[int] = 32

DEFAULT_JPEG_QUALITY: Final[int] = 80
MIN_JPEG_QUALITY: Final[int] = 30
MAX_JPEG_QUALITY: Final[int] = 95
DEFAULT_MAX_WIDTH: Final[int] = 1600
MIN_MAX_WIDTH: Final[int] = 320
MAX_MAX_WIDTH: Final[int] = 3840
DEFAULT_CONTROL_FPS: Final[int] = 8
DEFAULT_CONTROL_QUALITY: Final[int] = 55

#: 采不到屏时健康状态每 N 次失败打一条 warning，避免桌面锁屏时刷爆日志
#: （第一次失败与错误内容变化时总会立即记录，保证可观测）。
FAILURE_LOG_EVERY: Final[int] = 30


class CaptureError(RuntimeError):
    """采集/编码失败；由引擎转成健康状态，不让 worker 进程崩。"""


# --------------------------------------------------------------- 纯函数 ---
def signature_diff(previous: bytes | None, current: bytes) -> int:
    """两帧 64×36 灰度指纹的差异分（0..255，纯函数）。

    0.3.1（F36）：返回 ``max(top-k 平均, 全桌平均)``——top-k（``SIGNATURE_TOP_K``
    格）负责局部变化（小弹窗只污染少数格，全桌平均只有 ~0.26 分，远低于阈值 8，
    ``on_change_only`` 会漏检），全桌平均作为第二条件兜住"大面积缓慢渐变"这类
    每格都变化但都不大的场景。``previous`` 为空或长度不一致时返回
    ``SIGNATURE_CHANGED``：宁可多传一帧，也不要把"换了桌面分辨率"误判成静止。
    """

    if previous is None or not current:
        return SIGNATURE_CHANGED
    if len(previous) != len(current):
        return SIGNATURE_CHANGED
    diffs = sorted((abs(left - right) for left, right in zip(previous, current)), reverse=True)
    full_average = sum(diffs) / len(diffs)
    top_average = sum(diffs[:SIGNATURE_TOP_K]) / min(SIGNATURE_TOP_K, len(diffs))
    return int(max(top_average, full_average))


def is_changed(score: int, threshold: int) -> bool:
    """``score`` 是否达到变化阈值（纯函数）。"""

    return int(score) >= max(1, int(threshold))


def next_change_interval_s(streak: int) -> int:
    """连续静止 ``streak`` 次后的检查间隔：5 → 20 → 60（纯函数）。"""

    return CHANGE_INTERVALS_S[min(max(0, int(streak)), len(CHANGE_INTERVALS_S) - 1)]


def next_forced_interval_s(streak: int) -> int:
    """连续静止 ``streak`` 次后的强制出帧间隔：60 → 240 → 300（纯函数）。"""

    return FORCED_INTERVALS_S[min(max(0, int(streak)), len(FORCED_INTERVALS_S) - 1)]


def clamp_quality(value: Any, default: int = DEFAULT_JPEG_QUALITY) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_JPEG_QUALITY, min(MAX_JPEG_QUALITY, number))


def clamp_max_width(value: Any, default: int = DEFAULT_MAX_WIDTH) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_MAX_WIDTH, min(MAX_MAX_WIDTH, number))


# ------------------------------------------------------------ 配置快照 ---
@dataclass(frozen=True, slots=True)
class CaptureSettings:
    """采集相关配置的不可变快照（来自服务端设置，落地时做过钳制）。"""

    enabled: bool = True
    interval_s: int = 30
    on_change_only: bool = False
    change_threshold: int = 8
    jpeg_quality: int = DEFAULT_JPEG_QUALITY
    max_width: int = DEFAULT_MAX_WIDTH
    control_fps: int = DEFAULT_CONTROL_FPS
    control_quality: int = DEFAULT_CONTROL_QUALITY

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "CaptureSettings":
        """从服务端设置字典构造；缺键/类型错都回落到默认值，不抛异常。"""

        source = values or {}

        def flag(key: str, default: bool) -> bool:
            value = source.get(key, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return default

        def number(key: str, default: int, minimum: int, maximum: int) -> int:
            try:
                return max(minimum, min(maximum, int(source.get(key, default))))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=flag("capture_enabled", True),
            interval_s=number("capture_interval_s", 30, 5, 3600),
            on_change_only=flag("capture_on_change_only", False),
            change_threshold=number("capture_change_threshold", 8, 1, 64),
            jpeg_quality=clamp_quality(source.get("jpeg_quality", DEFAULT_JPEG_QUALITY)),
            max_width=clamp_max_width(source.get("max_width", DEFAULT_MAX_WIDTH)),
            control_fps=number("control_fps", DEFAULT_CONTROL_FPS, 1, 15),
            control_quality=clamp_quality(source.get("control_quality", DEFAULT_CONTROL_QUALITY)),
        )


# --------------------------------------------------------------- 决策 ---
@dataclass(frozen=True, slots=True)
class CaptureDecision:
    """一次"现在该不该抓"的决策（纯数据，便于单测断言）。"""

    action: str  # "skip" | "check" | "capture"
    reason: str  # disabled | control_active | forced | change_check | interval | waiting
    due_in_ms: int = 0

    @property
    def should_grab(self) -> bool:
        return self.action in {"check", "capture"}

    @property
    def forced(self) -> bool:
        return self.reason == "forced"


@dataclass
class CapturePolicy:
    """周期采集/变化退避的纯状态机（时间由 ``now_ms`` 注入）。

    状态只有三个数：连续静止次数、上次检查时刻、上次出帧时刻。没有计时器、
    没有线程、没有系统时钟，因此可以直接用合成时间轴单测。

    0.3.1（F36）：本对象实际被采集/控制/心跳/主循环等多个线程共享，状态读写
    都经 :attr:`_lock` 串行（``_streak += 1`` 是读改写，无锁会丢更新）；用
    ``RLock`` 是因为 ``evaluate`` 内部还要调 ``next_due_in_ms``。
    """

    settings: CaptureSettings = field(default_factory=CaptureSettings)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)
    _streak: int = field(default=0, init=False)
    _last_check_ms: int = field(default=0, init=False)
    _last_capture_ms: int = field(default=0, init=False)
    _control_active: bool = field(default=False, init=False)

    # -- 配置/状态 -------------------------------------------------------
    def update_settings(self, settings: CaptureSettings) -> None:
        with self._lock:
            self.settings = settings

    def set_control_active(self, active: bool) -> None:
        """实时控制流期间暂停周期采集（两次 DXGI grab 会互相抢带宽）。"""

        with self._lock:
            self._control_active = bool(active)

    @property
    def control_active(self) -> bool:
        with self._lock:
            return self._control_active

    @property
    def streak(self) -> int:
        with self._lock:
            return self._streak

    def reset(self) -> None:
        with self._lock:
            self._streak = 0
            self._last_check_ms = 0
            self._last_capture_ms = 0

    # -- 查询 ------------------------------------------------------------
    def next_due_in_ms(self, now_ms: int) -> int:
        """距离下一次需要动作还有多少毫秒（``0`` 表示现在就该动）。"""

        with self._lock:
            settings = self.settings
            if not settings.enabled or self._control_active:
                return 60_000
            if settings.on_change_only:
                next_forced = self._last_capture_ms + next_forced_interval_s(self._streak) * 1000
                next_check = self._last_check_ms + next_change_interval_s(self._streak) * 1000
                return max(0, min(next_forced, next_check) - int(now_ms))
            return max(0, self._last_capture_ms + settings.interval_s * 1000 - int(now_ms))

    def evaluate(self, now_ms: int) -> CaptureDecision:
        """判断此刻该做什么；纯函数式（只读状态）。"""

        with self._lock:
            now = int(now_ms)
            settings = self.settings
            if not settings.enabled:
                return CaptureDecision("skip", "disabled")
            if self._control_active:
                return CaptureDecision("skip", "control_active")

            if settings.on_change_only:
                if (
                    self._last_capture_ms == 0
                    or now - self._last_capture_ms >= next_forced_interval_s(self._streak) * 1000
                ):
                    return CaptureDecision("capture", "forced")
                if self._last_check_ms == 0 or now - self._last_check_ms >= next_change_interval_s(self._streak) * 1000:
                    return CaptureDecision("check", "change_check")
                return CaptureDecision("skip", "waiting", due_in_ms=self.next_due_in_ms(now))

            if self._last_capture_ms == 0 or now - self._last_capture_ms >= settings.interval_s * 1000:
                return CaptureDecision("capture", "interval")
            return CaptureDecision("skip", "waiting", due_in_ms=self.next_due_in_ms(now))

    # -- 状态推进 --------------------------------------------------------
    def record_unchanged(self, now_ms: int) -> None:
        """检查过但画面没变：静止次数 +1，下一次检查间隔升档。"""

        with self._lock:
            self._last_check_ms = int(now_ms)
            self._streak += 1

    def record_captured(self, now_ms: int, *, changed: bool) -> None:
        """出了一帧：画面变化时静止次数清零（间隔回落到 5s）。"""

        with self._lock:
            stamp = int(now_ms)
            self._last_check_ms = stamp
            self._last_capture_ms = stamp
            self._streak = 0 if changed else self._streak + 1


# --------------------------------------------------------------- 帧 ---
@dataclass(frozen=True, slots=True)
class CaptureFrame:
    """引擎产出的一帧（尚未上传）。"""

    payload: bytes
    width: int
    height: int
    device_ts_ms: int
    score: int
    kind: str = "screen"
    session_id: int = 0
    quality: int = DEFAULT_JPEG_QUALITY

    def size(self) -> int:
        return len(self.payload)


@dataclass(frozen=True, slots=True)
class CaptureHealth:
    """采集健康快照；服务端原样放进 heartbeat 的 ``state.capture``。"""

    state: str  # ok | starting | blocked | disabled | paused
    reason: str
    last_frame_ms: int
    last_error: str
    consecutive_failures: int
    frames: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "last_frame_ms": int(self.last_frame_ms),
            "last_error": self.last_error,
            "consecutive_failures": int(self.consecutive_failures),
            "frames": int(self.frames),
        }


# ------------------------------------------------------------- 帧源 ---
class FrameSource(Protocol):
    """抓一帧的实现（真实实现是 mss；单测注入假 source）。"""

    def grab(self, display: int = 0) -> Image.Image: ...

    def monitors(self) -> list[dict[str, Any]]: ...


class MssSource:
    """mss 抓屏实现：按线程持有 mss 实例（DXGI/GDI 上下文非线程安全）。"""

    def __init__(self) -> None:
        self._local = threading.local()

    def _engine(self) -> Any:
        engine = getattr(self._local, "engine", None)
        if engine is not None:
            return engine
        try:
            import mss
        except ImportError as exc:  # pragma: no cover - 依赖缺失只可能发生在打包错误
            raise CaptureError(f"缺少 mss 依赖，无法采集屏幕: {exc}") from exc
        try:
            engine = mss.mss()
        except Exception as exc:  # noqa: BLE001 - 无桌面/权限问题统一转 CaptureError
            raise CaptureError(f"无法初始化屏幕采集: {exc}") from exc
        self._local.engine = engine
        return engine

    def monitors(self) -> list[dict[str, Any]]:
        try:
            return [dict(item) for item in self._engine().monitors]
        except CaptureError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一错误类型
            raise CaptureError(f"枚举显示器失败: {exc}") from exc

    def grab(self, display: int = 0) -> Image.Image:
        engine = self._engine()
        try:
            monitors = engine.monitors
            index = 0
            if 0 <= display < len(monitors):
                index = display
            elif len(monitors) > 1:
                index = 1
            shot = engine.grab(monitors[index])
        except Exception as exc:  # noqa: BLE001 - 统一错误类型
            raise CaptureError(f"抓屏失败: {exc}") from exc
        try:
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as exc:  # noqa: BLE001 - 编码路径依赖的格式问题
            raise CaptureError(f"帧数据转换失败: {exc}") from exc


# ----------------------------------------------------------- 引擎 ---
class CaptureEngine:
    """唯一采集引擎：策略 + 抓屏 + 指纹 + JPEG + 健康。"""

    def __init__(
        self,
        *,
        source: FrameSource | None = None,
        settings: CaptureSettings | Mapping[str, Any] | None = None,
        clock: Any | None = None,
        display: int = 0,
    ) -> None:
        if isinstance(settings, CaptureSettings):
            resolved = settings
        else:
            resolved = CaptureSettings.from_mapping(settings)
        self._source: FrameSource = source or MssSource()
        self._policy = CapturePolicy(settings=resolved)
        #: ``clock`` 可注入（单测给固定/递增毫秒）；默认读系统时钟。
        self._clock = clock
        self._display = int(display)
        self._signature: bytes | None = None
        self._last_frame_ms = 0
        self._last_error = ""
        self._failures = 0
        self._frames = 0
        self._last_logged_error = ""
        # 0.3.1（F36）：连续失败退避计数（与 _failures 的健康语义分开维护，
        # 但两条失败路径都汇入这里，成功抓屏即清零）。
        self._backoff_failures = 0
        self._last_failure_ms = 0

    # -- 配置 ------------------------------------------------------------
    @property
    def settings(self) -> CaptureSettings:
        return self._policy.settings

    @property
    def policy(self) -> CapturePolicy:
        return self._policy

    def apply_settings(self, values: CaptureSettings | Mapping[str, Any]) -> None:
        """应用新配置；关掉再打开采集时清空指纹，避免跨配置误判。"""

        resolved = values if isinstance(values, CaptureSettings) else CaptureSettings.from_mapping(values)
        self._policy.update_settings(resolved)
        if not resolved.enabled:
            self._signature = None

    def set_control_active(self, active: bool) -> None:
        self._policy.set_control_active(active)

    def reset_signature(self) -> None:
        self._signature = None

    def now_ms(self) -> int:
        if self._clock is not None:
            return int(self._clock())
        return int(time.time() * 1000)

    # -- 采集 ------------------------------------------------------------
    def poll(self, *, now_ms: int | None = None) -> CaptureFrame | None:
        """周期采集入口：按策略决定抓/不抓，返回要上传的帧或 ``None``。"""

        stamp = self.now_ms() if now_ms is None else int(now_ms)
        decision = self._policy.evaluate(stamp)
        if not decision.should_grab:
            return None
        try:
            image = self._source.grab(self._display)
            signature = self._fingerprint(image)
            score = signature_diff(self._signature, signature)
            self._signature = signature
            if decision.action == "check" and not is_changed(score, self.settings.change_threshold):
                self._policy.record_unchanged(stamp)
                self._mark_grab_ok()
                log.debug("画面未变化（score=%s < %s），跳过采集", score, self.settings.change_threshold)
                return None
            payload, width, height = self._encode(image, self.settings.jpeg_quality)
        except CaptureError as exc:
            self._mark_failure(stamp, str(exc))
            return None
        changed = is_changed(score, self.settings.change_threshold)
        self._policy.record_captured(stamp, changed=changed)
        self._mark_frame(stamp)
        log.debug(
            "采集成功 kind=%s score=%s bytes=%s reason=%s",
            "screen",
            score,
            len(payload),
            decision.reason,
        )
        return CaptureFrame(
            payload=payload,
            width=width,
            height=height,
            device_ts_ms=stamp,
            score=score,
            kind="screen",
            quality=self.settings.jpeg_quality,
        )

    def capture_now(self, *, now_ms: int | None = None, session_id: int = 0) -> CaptureFrame:
        """命令触发的一次性截图（``screenshot``/``collect_now``）：总是出帧。"""

        stamp = self.now_ms() if now_ms is None else int(now_ms)
        image = self._source.grab(self._display)
        try:
            signature = self._fingerprint(image)
            score = signature_diff(self._signature, signature)
            self._signature = signature
            payload, width, height = self._encode(image, self.settings.jpeg_quality)
        except CaptureError:
            raise
        except Exception as exc:  # noqa: BLE001 - 编码异常同样转 CaptureError
            raise CaptureError(f"编码帧失败: {exc}") from exc
        self._policy.record_captured(stamp, changed=True)
        self._mark_frame(stamp)
        return CaptureFrame(
            payload=payload,
            width=width,
            height=height,
            device_ts_ms=stamp,
            score=score,
            kind="screen",
            session_id=int(session_id),
            quality=self.settings.jpeg_quality,
        )

    def capture_control_frame(
        self,
        *,
        now_ms: int | None = None,
        session_id: int = 0,
        quality: int | None = None,
    ) -> CaptureFrame:
        """实时控制流的一帧：低质量、不参与周期策略/指纹，避免拖慢采集。

        ``quality`` 允许本次控制会话覆盖配置值（服务端 control 信封的 quality）。
        """

        stamp = self.now_ms() if now_ms is None else int(now_ms)
        target_quality = clamp_quality(quality, self.settings.control_quality)
        image = self._source.grab(self._display)
        try:
            payload, width, height = self._encode(image, target_quality)
        except CaptureError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一错误类型
            raise CaptureError(f"编码控制帧失败: {exc}") from exc
        self._mark_frame(stamp)
        return CaptureFrame(
            payload=payload,
            width=width,
            height=height,
            device_ts_ms=stamp,
            score=0,
            kind="control",
            session_id=int(session_id),
            quality=target_quality,
        )

    def next_due_in_ms(self, now_ms: int | None = None) -> int:
        """worker 循环用它决定睡多久，避免固定 1s 空转。

        0.3.1（F36）：连续失败时叠加退避阶梯（0.5s → 2s → 10s 封顶）——
        采不到屏（锁屏/无桌面）时策略本身可能给出 0ms，没有退避就会空转 grab。
        """

        stamp = self.now_ms() if now_ms is None else int(now_ms)
        due = self._policy.next_due_in_ms(stamp)
        failures = self._backoff_failures
        if failures <= 0:
            return due
        backoff_ms = FAILURE_BACKOFF_MS[min(failures - 1, len(FAILURE_BACKOFF_MS) - 1)]
        remaining = backoff_ms - (stamp - self._last_failure_ms)
        return max(due, max(0, remaining))

    def record_failure(self, error: str = "", *, now_ms: int | None = None) -> None:
        """0.3.1（F36）：记录一次采集失败（退避阶梯的输入）。

        引擎内部的 ``CaptureError``（经 :meth:`_mark_failure`）与 runtime 采集
        循环的兜底异常路径都要汇入这里；``error`` 非空时同时计入健康状态。
        """

        stamp = self.now_ms() if now_ms is None else int(now_ms)
        if error:
            self._mark_failure(stamp, error)
        else:
            self._record_backoff_failure(stamp)

    def monitors(self) -> list[dict[str, Any]]:
        try:
            return self._source.monitors()
        except CaptureError as exc:
            self._mark_failure(self.now_ms(), str(exc))
            return []

    # -- 健康 ------------------------------------------------------------
    def health(self, *, now_ms: int | None = None) -> CaptureHealth:
        settings = self.settings
        if not settings.enabled:
            state, reason = "disabled", "采集已关闭"
        elif self._policy.control_active:
            state, reason = "paused", "实时控制进行中，周期采集暂停"
        elif self._failures > 0:
            state, reason = "blocked", self._last_error or "采集持续失败"
        elif self._last_frame_ms == 0:
            state, reason = "starting", "尚未产出首帧"
        else:
            state, reason = "ok", ""
        return CaptureHealth(
            state=state,
            reason=reason,
            last_frame_ms=self._last_frame_ms,
            last_error=self._last_error,
            consecutive_failures=self._failures,
            frames=self._frames,
        )

    def health_dict(self, *, now_ms: int | None = None) -> dict[str, Any]:
        return self.health(now_ms=now_ms).to_dict()

    # -- 内部 ------------------------------------------------------------
    def _fingerprint(self, image: Image.Image) -> bytes:
        try:
            small = image.convert("L").resize(
                (SIGNATURE_WIDTH, SIGNATURE_HEIGHT), Image.Resampling.BILINEAR
            )
        except Exception as exc:  # noqa: BLE001 - 异常图像数据统一转 CaptureError
            raise CaptureError(f"生成变化指纹失败: {exc}") from exc
        return small.tobytes()

    def _encode(self, image: Image.Image, quality: int) -> tuple[bytes, int, int]:
        try:
            width, height = image.size
            max_width = self.settings.max_width
            if max_width and width > max_width:
                target_height = max(1, round(height * max_width / width))
                image = image.resize((max_width, target_height), Image.Resampling.BILINEAR)
                width, height = image.size
            buffer = io.BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=clamp_quality(quality),
                optimize=False,  # 优化编码会显著增加 CPU，采集路径要的是帧成本
            )
        except Exception as exc:  # noqa: BLE001 - 统一错误类型
            raise CaptureError(f"JPEG 编码失败: {exc}") from exc
        return buffer.getvalue(), width, height

    def _record_backoff_failure(self, stamp: int) -> None:
        """0.3.1（F36）：推进失败退避计数（不触碰健康状态）。"""

        self._backoff_failures += 1
        self._last_failure_ms = int(stamp)

    def _mark_grab_ok(self) -> None:
        self._failures = 0
        self._backoff_failures = 0  # 0.3.1（F36）：抓屏成功即清零退避
        self._last_error = ""

    def _mark_frame(self, stamp: int) -> None:
        self._mark_grab_ok()
        self._last_frame_ms = int(stamp)
        self._frames += 1

    def _mark_failure(self, stamp: int, error: str) -> None:
        self._failures += 1
        self._last_error = error
        self._record_backoff_failure(stamp)  # 0.3.1（F36）：引擎内部失败同样进入退避
        repeated = error == self._last_logged_error
        if not repeated or self._failures % FAILURE_LOG_EVERY == 0:
            log.warning(
                "采集失败（连续 %s 次，ts=%s）: %s",
                self._failures,
                stamp,
                error,
            )
            self._last_logged_error = error


__all__ = [
    "CHANGE_INTERVALS_S",
    "DEFAULT_CONTROL_FPS",
    "DEFAULT_CONTROL_QUALITY",
    "DEFAULT_JPEG_QUALITY",
    "DEFAULT_MAX_WIDTH",
    "FAILURE_BACKOFF_MS",
    "FORCED_INTERVALS_S",
    "SIGNATURE_CHANGED",
    "SIGNATURE_HEIGHT",
    "SIGNATURE_TOP_K",
    "SIGNATURE_WIDTH",
    "CaptureDecision",
    "CaptureEngine",
    "CaptureError",
    "CaptureFrame",
    "CaptureHealth",
    "CapturePolicy",
    "CaptureSettings",
    "FrameSource",
    "MssSource",
    "clamp_max_width",
    "clamp_quality",
    "is_changed",
    "next_change_interval_s",
    "next_forced_interval_s",
    "signature_diff",
]