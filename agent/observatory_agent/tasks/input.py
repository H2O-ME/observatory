"""输入注入：SendInput + 虚拟桌面坐标 + PerMonitorV2 DPI 感知。

为什么旧实现错
--------------

* **A16 多显示器坐标**：旧 ``input.py:114-129`` 用 ``GetSystemMetrics(0/1)``（**主显示器**
  的尺寸）把坐标归一化，却同时设置了 ``MOUSEEVENTF_VIRTUALDESK``。``VIRTUALDESK`` 的语义是
  “0..65535 覆盖整个**虚拟桌面**（所有显示器，原点可能为负）”，双屏时右半屏点击会偏到
  另一块屏；而采集侧 ``capture.py`` 抓的是“所有显示器拼合图”，两侧坐标系本来就不一致。
  0.3.0 改成：坐标换算只依赖 :class:`VirtualDesktop`（原点/宽高），且换算函数是纯函数，
  可在 Linux CI 上用双屏负坐标、150% DPI 的布局做 ≤2px 的单测。
* **DPI 感知**：旧代码全仓库没有 ``SetProcessDpiAwareness``，worker 是 DPI-unaware 进程，
  高 DPI 下 ``GetSystemMetrics`` 与 ``mss`` 拿到的尺寸都可能被虚拟化。现在 worker 启动时
  显式切 PerMonitorV2（失败时逐级回退并记录日志）。
* 旧 ``input.py:100-103`` 的 ``_mouse_event`` 返回同一个 ``INPUT`` 两次、无人调用，已删除；
  旧 ``type_text`` 用 ``ord(char)`` 填 ``wScan``（WORD），emoji 等非 BMP 字符被截断，现在
  先编码成 UTF-16 code unit 序列再发 ``KEYEVENTF_UNICODE``。
"""

from __future__ import annotations

import ctypes
import logging
import math
import sys
import threading
from collections.abc import Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable

from ..win import WindowsOnlyOperation

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ virtual --
#: ``GetSystemMetrics`` 的虚拟桌面索引（SM_XVIRTUALSCREEN…）。
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1

#: ``SetProcessDpiAwarenessContext`` 的 PerMonitorV2 上下文（值为 -4）。
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
#: ``SetProcessDpiAwareness`` 的 PROCESS_PER_MONITOR_DPI_AWARE。
PROCESS_PER_MONITOR_DPI_AWARE = 2

#: SendInput 的绝对坐标范围是 0..65535（闭区间）的虚拟桌面比例。
ABSOLUTE_MAX = 65535


@dataclass(frozen=True)
class Monitor:
    """一块显示器的物理像素边界。"""

    left: int
    top: int
    width: int
    height: int
    primary: bool = False

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"显示器尺寸必须为正: {self.width}x{self.height}")


@dataclass(frozen=True)
class Point:
    """虚拟桌面像素坐标（A16 单测断言 ``point.x`` / ``point.y``）。"""

    x: int
    y: int


@dataclass(frozen=True)
class VirtualDesktop:
    """由多块显示器拼合出的虚拟桌面，单位为**物理像素**（PerMonitorV2 下即真实像素）。"""

    monitors: tuple[Monitor, ...]

    def __post_init__(self) -> None:
        normalized = tuple(
            item
            if isinstance(item, Monitor)
            else Monitor(
                left=int(item["left"]),
                top=int(item["top"]),
                width=int(item["width"]),
                height=int(item["height"]),
                primary=bool(item.get("primary", False)),
            )
            for item in self.monitors
        )
        if not normalized:
            raise ValueError("VirtualDesktop 至少需要一块显示器")
        object.__setattr__(self, "monitors", normalized)

    @classmethod
    def from_monitors(cls, monitors: Sequence[Mapping[str, Any] | Monitor]) -> "VirtualDesktop":
        return cls(monitors=tuple(monitors))

    @classmethod
    def from_bounds(cls, *, left: int, top: int, width: int, height: int) -> "VirtualDesktop":
        return cls(monitors=(Monitor(left=left, top=top, width=width, height=height, primary=True),))

    @property
    def left(self) -> int:
        return min(monitor.left for monitor in self.monitors)

    @property
    def top(self) -> int:
        return min(monitor.top for monitor in self.monitors)

    @property
    def right(self) -> int:
        return max(monitor.left + monitor.width for monitor in self.monitors)

    @property
    def bottom(self) -> int:
        return max(monitor.top + monitor.height for monitor in self.monitors)

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def primary(self) -> Monitor | None:
        for monitor in self.monitors:
            if monitor.primary:
                return monitor
        return None


def clamp_unit(value: float) -> float:
    """把归一化坐标夹到 [0, 1]；非有限值抛 ValueError（不做静默兜底）。"""

    if not math.isfinite(value):
        raise ValueError(f"归一化坐标必须是有限数，收到 {value!r}")
    return max(0.0, min(1.0, value))


def normalized_to_virtual(nx: float, ny: float, desktop: VirtualDesktop) -> tuple[int, int]:
    """归一化坐标（0..1）→ 虚拟桌面像素坐标。

    用 ``width - 1`` / ``height - 1`` 作为跨度，保证 0→left、1→right-1，与
    ``MOUSEEVENTF_VIRTUALDESK`` 的闭区间映射一致（误差 ≤1px）。
    """

    x = clamp_unit(nx) * (desktop.width - 1)
    y = clamp_unit(ny) * (desktop.height - 1)
    return desktop.left + int(round(x)), desktop.top + int(round(y))


def normalized_10000_to_virtual(nx: float, ny: float, desktop: VirtualDesktop) -> tuple[int, int]:
    """0..10000 归一化坐标（旧控制台协议）→ 虚拟桌面像素。"""

    return normalized_to_virtual(clamp_unit(nx / 10_000.0), clamp_unit(ny / 10_000.0), desktop)


def virtual_to_absolute(x: int, y: int, desktop: VirtualDesktop) -> tuple[int, int]:
    """虚拟桌面像素 → ``SendInput`` 的 0..65535 绝对坐标（纯函数，A16 的单测对象）。"""

    if desktop.width <= 1:
        absolute_x = 0
    else:
        clamped_x = max(desktop.left, min(desktop.right - 1, int(x)))
        absolute_x = int(round((clamped_x - desktop.left) * ABSOLUTE_MAX / (desktop.width - 1)))
    if desktop.height <= 1:
        absolute_y = 0
    else:
        clamped_y = max(desktop.top, min(desktop.bottom - 1, int(y)))
        absolute_y = int(round((clamped_y - desktop.top) * ABSOLUTE_MAX / (desktop.height - 1)))
    return (
        max(0, min(ABSOLUTE_MAX, absolute_x)),
        max(0, min(ABSOLUTE_MAX, absolute_y)),
    )


def normalized_to_absolute(nx: float, ny: float, desktop: VirtualDesktop) -> tuple[int, int]:
    """归一化坐标（0..1）→ SendInput 绝对坐标。"""

    return virtual_to_absolute(*normalized_to_virtual(nx, ny, desktop), desktop)


def to_absolute(
    desktop: VirtualDesktop,
    nx: float,
    ny: float,
    *,
    dpi_scale: float = 1.0,
) -> Point:
    """捕获帧归一化坐标（0..1）→ 虚拟桌面**物理像素**（A16 的单测入口）。

    ``dpi_scale`` 只做参数校验，**不参与换算**：PerMonitorV2 下 ``mss`` 抓到的是物理像素，
    ``GetSystemMetrics(SM_*VIRTUALSCREEN)`` 返回的也是物理像素，再乘一次 DPI 就是双重缩放
    （0.2.10 的坐标漂移正是这一类错误）。150% DPI 的单屏用例因此必须得到 960 而不是 1440。
    """

    if not math.isfinite(dpi_scale) or dpi_scale <= 0:
        raise ValueError(f"dpi_scale 必须为正的有限数，收到 {dpi_scale!r}")
    x, y = normalized_to_virtual(nx, ny, desktop)
    return Point(x=x, y=y)


def to_sendinput(desktop: VirtualDesktop, point: Point) -> tuple[int, int]:
    """虚拟桌面像素 → ``SendInput`` 绝对坐标（``MOUSEEVENTF_VIRTUALDESK`` 的 0..65535）。"""

    return virtual_to_absolute(point.x, point.y, desktop)


#: 旧控制台用 0..10000 表达归一化坐标；``to_normalized``/``from_normalized`` 是它与
#: SendInput 的 0..65535 绝对坐标之间的无损（±1）互转，供协议适配层复用。
NORMALIZED_MAX = 10_000


def to_normalized(value: int) -> int:
    """0..10000 归一化值 → 0..65535 绝对坐标值。"""

    clamped = max(0, min(NORMALIZED_MAX, int(value)))
    return int(round(clamped * ABSOLUTE_MAX / NORMALIZED_MAX))


def from_normalized(value: int) -> int:
    """0..65535 绝对坐标值 → 0..10000 归一化值。"""

    clamped = max(0, min(ABSOLUTE_MAX, int(value)))
    return int(round(clamped * NORMALIZED_MAX / ABSOLUTE_MAX))


def virtual_desktop() -> VirtualDesktop:
    """读取当前进程的虚拟桌面边界（DPI-aware 时是物理像素）。"""

    if sys.platform != "win32":
        raise WindowsOnlyOperation("读取虚拟桌面尺寸")
    try:
        user32 = ctypes.WinDLL("user32")
        width = int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
        height = int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
        left = int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
        top = int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
    except Exception as exc:  # noqa: BLE001 - 取不到就明确失败，不猜 1920x1080
        raise WindowsOnlyOperation(f"读取虚拟桌面失败: {exc}") from exc
    if width <= 0 or height <= 0:
        width = max(1, int(user32.GetSystemMetrics(_SM_CXSCREEN)))
        height = max(1, int(user32.GetSystemMetrics(_SM_CYSCREEN)))
        left, top = 0, 0
    return VirtualDesktop.from_bounds(left=left, top=top, width=width, height=height)


def enable_dpi_awareness() -> str:
    """把当前进程切到 PerMonitorV2；返回实际生效的机制（便于日志/诊断）。

    顺序：``SetProcessDpiAwarenessContext(-4)`` → ``shcore.SetProcessDpiAwareness(2)`` →
    ``user32.SetProcessDPIAware()``。全部失败时返回 ``"none"`` 并记录 warning（不静默）。
    """

    if sys.platform != "win32":
        raise WindowsOnlyOperation("设置 DPI 感知")
    try:
        user32 = ctypes.WinDLL("user32")
        context = ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        if user32.SetProcessDpiAwarenessContext(context):
            return "per-monitor-v2"
    except (AttributeError, OSError) as exc:
        log.debug("SetProcessDpiAwarenessContext unavailable: %s", exc)
    try:
        shcore = ctypes.WinDLL("shcore")
        if shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return "per-monitor"
    except (AttributeError, OSError) as exc:
        log.debug("SetProcessDpiAwareness unavailable: %s", exc)
    try:
        user32 = ctypes.WinDLL("user32")
        if user32.SetProcessDPIAware():
            return "system-aware"
    except (AttributeError, OSError) as exc:
        log.debug("SetProcessDPIAware unavailable: %s", exc)
    log.warning("could not enable DPI awareness; high-DPI coordinates may drift")
    return "none"


# ------------------------------------------------------------------ structs --
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120
ULONG_PTR = ctypes.c_size_t

#: 需要 ``KEYEVENTF_EXTENDEDKEY`` 的虚拟键（右修饰键、方向键、Win 键等）。
EXTENDED_KEYS = frozenset(
    {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0x91, 0xA3, 0xA5}
)

BUTTON_FLAGS = {
    1: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    2: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    3: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

#: SendInput 每批最多事件数（避免单次调用过大）。
MAX_BATCH = 64


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class INPUT_UNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("union", INPUT_UNION))


def _user32():
    if sys.platform != "win32":
        raise WindowsOnlyOperation("发送输入事件")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    return user32


def _send(*inputs: INPUT) -> bool:
    if not inputs:
        log.debug("SendInput called with no events")
        return False
    user32 = _user32()
    batch = (INPUT * len(inputs))(*inputs)
    sent = int(user32.SendInput(len(inputs), batch, ctypes.sizeof(INPUT)))
    if sent != len(inputs):
        log.warning("SendInput sent %s/%s events (error=%s)", sent, len(inputs), ctypes.get_last_error())
        return False
    return True


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    item = INPUT(type=INPUT_MOUSE)
    item.union.mi = MOUSEINPUT(
        dx=int(dx), dy=int(dy), mouseData=int(data) & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0
    )
    return item


def _key_input(vk: int, *, down: bool, scan: int = 0, unicode: bool = False) -> INPUT:
    flags = 0
    if unicode:
        flags |= KEYEVENTF_UNICODE
    elif vk in EXTENDED_KEYS:
        flags |= KEYEVENTF_EXTENDEDKEY
    if not down:
        flags |= KEYEVENTF_KEYUP
    item = INPUT(type=INPUT_KEYBOARD)
    item.union.ki = KEYBDINPUT(wVk=int(vk), wScan=int(scan), dwFlags=flags, time=0, dwExtraInfo=0)
    return item


def text_to_utf16_units(text: str) -> tuple[int, ...]:
    """把文本编码成 UTF-16 code unit 序列（emoji 会变成两个 unit；纯函数）。"""

    if not text:
        return ()
    encoded = text.encode("utf-16-le", "surrogatepass")
    return tuple(int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2))


# --------------------------------------------------------------------- mice --
def mouse_move(
    x: int,
    y: int,
    *,
    desktop: VirtualDesktop | None = None,
    desktop_provider: Callable[[], VirtualDesktop] = virtual_desktop,
) -> bool:
    """把鼠标移到**虚拟桌面**像素坐标 ``(x, y)``。"""

    layout = desktop if desktop is not None else desktop_provider()
    absolute_x, absolute_y = virtual_to_absolute(x, y, layout)
    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    return _send(_mouse_input(flags, absolute_x, absolute_y))


def mouse_move_normalized(
    nx: float,
    ny: float,
    *,
    desktop: VirtualDesktop | None = None,
    desktop_provider: Callable[[], VirtualDesktop] = virtual_desktop,
) -> bool:
    """把鼠标移到归一化坐标（0..1，对应收到的采集帧）。"""

    layout = desktop if desktop is not None else desktop_provider()
    x, y = normalized_to_virtual(nx, ny, layout)
    return mouse_move(x, y, desktop=layout)


def mouse_button(button: int, down: bool) -> bool:
    flags = BUTTON_FLAGS.get(int(button) or 1)
    if flags is None:
        log.warning("unsupported mouse button: %s", button)
        return False
    return _send(_mouse_input(flags[0] if down else flags[1]))


def mouse_wheel(delta: int, *, horizontal: bool = False) -> bool:
    flags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    return _send(_mouse_input(flags, data=int(delta) * WHEEL_DELTA))


# ----------------------------------------------------------------- keyboard --
def key_event(vk: int, down: bool) -> bool:
    if not vk:
        log.warning("key_event called with vk=0")
        return False
    return _send(_key_input(int(vk), down=down))


def type_text(text: str) -> bool:
    """按 Unicode 注入文本（非 BMP 字符发 surrogate pair 两个 unit）。"""

    units = text_to_utf16_units(text)
    if not units:
        return False
    events: list[INPUT] = []
    for unit in units:
        events.append(_key_input(0, down=True, scan=unit, unicode=True))
        events.append(_key_input(0, down=False, scan=unit, unicode=True))
    for start in range(0, len(events), MAX_BATCH):
        if not _send(*events[start : start + MAX_BATCH]):
            return False
    return True


# ---------------------------------------------------------------- dispatch ---
class InputState:
    """已按下键/鼠标按钮的账本（A3，线程安全）。

    为什么必须有它：``SendInput`` 的 keydown 是**系统全局状态**，没有任何
    "会话结束就自动抬起"的机制。控制会话被 stop / TTL 到期 / 链路断开时如果不
    显式发 keyup，远端机器就会一直保持按住（实测：Ctrl 键卡住、鼠标左键卡住，
    用户只能重启）。旧实现（``tasks/input.py`` 与 ``runtime._stop_control``）
    完全没有这份状态，所以永远不知道要抬哪些键。

    ``release_all`` 是唯一的释放入口：stop、TTL 到期、管道断开、worker 退出
    全部走它，保证"无论从哪条路径结束控制，按键都被抬起"。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: set[int] = set()
        self._buttons: set[int] = set()

    # -- 记账 ------------------------------------------------------------
    def key_down(self, vk: int) -> None:
        if vk:
            with self._lock:
                self._keys.add(int(vk))

    def key_up(self, vk: int) -> None:
        with self._lock:
            self._keys.discard(int(vk))

    def button_down(self, button: int) -> None:
        with self._lock:
            self._buttons.add(int(button) or 1)

    def button_up(self, button: int) -> None:
        with self._lock:
            self._buttons.discard(int(button) or 1)

    # -- 只读 ------------------------------------------------------------
    def snapshot(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """当前按住的 ``(键, 鼠标按钮)``；调用方可用于日志/断言。"""

        with self._lock:
            return tuple(sorted(self._keys)), tuple(sorted(self._buttons))

    def pending_count(self) -> int:
        with self._lock:
            return len(self._keys) + len(self._buttons)

    def clear(self) -> None:
        """只清账本、不注入（测试/异常兜底用；正常路径用 ``release_all``）。"""

        with self._lock:
            self._keys.clear()
            self._buttons.clear()

    # -- 释放 ------------------------------------------------------------
    def release_all(self, *, sender: Callable[[tuple[INPUT, ...]], bool] | None = None) -> int:
        """抬起所有按住的键与鼠标按钮，返回释放成功的数量。

        先发 up 再清账本：注入失败时**保留**记录，下次 cleanup 还会重试——
        宁可重复发一次 keyup（幂等，无副作用），也不能漏掉一个还按着的键。
        """

        send = sender if sender is not None else _send
        with self._lock:
            keys = sorted(self._keys)
            buttons = sorted(self._buttons)
        released = 0
        failed_keys: set[int] = set()
        failed_buttons: set[int] = set()
        for vk in keys:
            try:
                ok = bool(send((_key_input(vk, down=False),)))
            except Exception:  # noqa: BLE001 - 注入不可用（非 Windows）时仍要清账本
                log.warning("释放按键 %s 时注入失败", vk, exc_info=True)
                ok = True  # 环境不支持注入：不保留，避免每次 cleanup 重复刷日志
            if ok:
                released += 1
            else:
                failed_keys.add(vk)
        for button in buttons:
            flags = BUTTON_FLAGS.get(button)
            try:
                ok = bool(flags) and bool(send((_mouse_input(flags[1]),)))
            except Exception:  # noqa: BLE001 - 同上
                log.warning("释放鼠标按钮 %s 时注入失败", button, exc_info=True)
                ok = True
            if ok:
                released += 1
            else:
                failed_buttons.add(button)
        with self._lock:
            self._keys = failed_keys
            self._buttons = failed_buttons
        if keys or buttons:
            log.info(
                "控制结束：释放按住的 %s 个键、%s 个鼠标按钮（失败保留 %s/%s）",
                len(keys),
                len(buttons),
                len(failed_keys),
                len(failed_buttons),
            )
        return released


#: 进程级默认账本（``handle_event`` 的默认 ``state``；worker 也可以注入自己的）。
DEFAULT_INPUT_STATE = InputState()


def release_all_inputs(*, state: InputState | None = None) -> int:
    """抬起所有按住的键/按钮（A3 的统一出口，供 runtime 调用）。"""

    return (state if state is not None else DEFAULT_INPUT_STATE).release_all()


def event_target(
    event: dict[str, Any],
    *,
    desktop: VirtualDesktop | None = None,
    desktop_provider: Callable[[], VirtualDesktop] = virtual_desktop,
) -> tuple[int, int]:
    """把控制台事件里的坐标换算成虚拟桌面像素（纯函数，单独可测）。

    支持三种输入约定（同时存在时以 ``normalized`` 优先）：

    * ``{"x": 1920, "y": 540}``：已经是虚拟桌面像素；
    * ``{"normalized": True, "x": 0.5, "y": 0.5}``：0..1 归一化（0.3.0 协议）；
    * ``{"norm": True, "x": 5000, "y": 5000}``：0..10000 归一化（0.2.x 控制台）。
    """

    layout = desktop if desktop is not None else desktop_provider()
    x = float(event.get("x", 0))
    y = float(event.get("y", 0))
    if event.get("normalized"):
        return normalized_to_virtual(x, y, layout)
    if event.get("norm"):
        return normalized_10000_to_virtual(x, y, layout)
    return int(round(x)), int(round(y))


def handle_event(
    event: dict[str, Any],
    *,
    desktop: VirtualDesktop | None = None,
    desktop_provider: Callable[[], VirtualDesktop] = virtual_desktop,
    state: "InputState | None" = None,
) -> bool:
    """执行一条控制台事件；返回是否被理解并成功注入。

    ``state`` 用于记录"当前按住了哪些键/鼠标按钮"（A3）。``down`` 事件注入成功
    后记账、``up`` 事件注入成功后销账；控制结束/租约到期/断链时由
    :meth:`InputState.release_all` 统一抬起——否则远端会永久保持按下（实测
    卡键：前端在鼠标离开控制区时收不到 keyup）。
    """

    ledger = state if state is not None else DEFAULT_INPUT_STATE
    kind = event.get("kind")
    if kind == "mouse":
        x, y = event_target(event, desktop=desktop, desktop_provider=desktop_provider)
        if not mouse_move(x, y, desktop=desktop if desktop is not None else desktop_provider()):
            return False
        action = str(event.get("action") or "move")
        if action == "move":
            return True
        button = int(event.get("button", 1))
        if action == "click":
            pressed = mouse_button(button, True)
            if pressed:
                ledger.button_down(button)
            released = mouse_button(button, False)
            if released:
                ledger.button_up(button)
            return pressed and released
        if action in {"down", "up"}:
            ok = mouse_button(button, action == "down")
            if ok:
                ledger.button_down(button) if action == "down" else ledger.button_up(button)
            return ok
        log.warning("unsupported mouse action: %s", action)
        return False
    if kind == "wheel":
        return mouse_wheel(int(event.get("delta", 0)), horizontal=bool(event.get("horizontal")))
    if kind == "key":
        code = int(event.get("code", 0))
        down = bool(event.get("down", True))
        ok = key_event(code, down)
        if ok:
            ledger.key_down(code) if down else ledger.key_up(code)
        return ok
    if kind == "text":
        return type_text(str(event.get("text", "")))
    log.warning("unsupported input event kind: %s", kind)
    return False


__all__ = [
    "ABSOLUTE_MAX",
    "BUTTON_FLAGS",
    "DEFAULT_INPUT_STATE",
    "DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2",
    "EXTENDED_KEYS",
    "INPUT",
    "InputState",
    "MAX_BATCH",
    "Monitor",
    "NORMALIZED_MAX",
    "Point",
    "VirtualDesktop",
    "clamp_unit",
    "enable_dpi_awareness",
    "event_target",
    "from_normalized",
    "handle_event",
    "key_event",
    "mouse_button",
    "mouse_move",
    "mouse_move_normalized",
    "mouse_wheel",
    "normalized_10000_to_virtual",
    "normalized_to_absolute",
    "normalized_to_virtual",
    "release_all_inputs",
    "text_to_utf16_units",
    "to_absolute",
    "to_normalized",
    "to_sendinput",
    "type_text",
    "virtual_desktop",
    "virtual_to_absolute",
]