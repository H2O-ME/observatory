"""Input injection for the session this worker runs in.

Uses ``SendInput`` (not the legacy ``mouse_event``) because it composes correctly
with the session's input queue and respects UIPI. Events arrive already normalised
from the console: mouse coordinates are in *screen* pixels and converted to the
0..65535 absolute space SendInput expects.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

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
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

ULONG_PTR = ctypes.c_size_t


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


BUTTON_FLAGS = {
    1: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    2: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    3: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _send(*inputs: INPUT) -> bool:
    if not inputs:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        array = (INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), ctypes.byref(array), ctypes.sizeof(INPUT))
        if sent != len(inputs):
            log.warning("SendInput sent %s/%s (error %s)", sent, len(inputs), ctypes.get_last_error())
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("SendInput failed: %s", exc)
        return False


def _mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> tuple[INPUT, INPUT]:
    item = INPUT(type=INPUT_MOUSE)
    item.union.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=0)
    return item, item


def _screen_size() -> tuple[int, int]:
    try:
        user32 = ctypes.WinDLL("user32")
        return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
    except Exception:  # noqa: BLE001
        return 1920, 1080


def mouse_move(x: int, y: int, *, width: int = 0, height: int = 0) -> bool:
    screen_w, screen_h = (width, height) if width and height else _screen_size()
    if screen_w <= 0 or screen_h <= 0:
        return False
    nx = int(max(0, min(screen_w - 1, x)) * 65535 / max(1, screen_w - 1))
    ny = int(max(0, min(screen_h - 1, y)) * 65535 / max(1, screen_h - 1))
    item = INPUT(type=INPUT_MOUSE)
    item.union.mi = MOUSEINPUT(
        dx=nx,
        dy=ny,
        mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        time=0,
        dwExtraInfo=0,
    )
    return _send(item)


def mouse_button(button: int, down: bool) -> bool:
    flags = BUTTON_FLAGS.get(int(button) or 1)
    if flags is None:
        return False
    item = INPUT(type=INPUT_MOUSE)
    item.union.mi = MOUSEINPUT(
        dx=0, dy=0, mouseData=0, dwFlags=flags[0] if down else flags[1], time=0, dwExtraInfo=0
    )
    return _send(item)


def mouse_wheel(delta: int) -> bool:
    item = INPUT(type=INPUT_MOUSE)
    item.union.mi = MOUSEINPUT(
        dx=0, dy=0, mouseData=(int(delta) * WHEEL_DELTA) & 0xFFFFFFFF, dwFlags=MOUSEEVENTF_WHEEL, time=0, dwExtraInfo=0
    )
    return _send(item)


def key_event(vk: int, down: bool) -> bool:
    if not vk:
        return False
    flags = KEYEVENTF_KEYUP if not down else 0
    if vk in EXTENDED_KEYS:
        flags |= KEYEVENTF_EXTENDEDKEY
    item = INPUT(type=INPUT_KEYBOARD)
    item.union.ki = KEYBDINPUT(wVk=int(vk), wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return _send(item)


def type_text(text: str) -> bool:
    if not text:
        return False
    inputs: list[INPUT] = []
    for char in text:
        code = ord(char)
        down = INPUT(type=INPUT_KEYBOARD)
        down.union.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0)
        up = INPUT(type=INPUT_KEYBOARD)
        up.union.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
        inputs.extend([down, up])
    for index in range(0, len(inputs), 32):
        if not _send(*inputs[index : index + 32]):
            return False
    return True


#: Virtual keys that need the extended-key flag (right-hand modifiers, arrows...).
EXTENDED_KEYS = frozenset(
    {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0x91, 0xA3, 0xA5}
)


def handle_event(event: dict) -> bool:
    """Apply one console event. Returns True when it was understood.

    Mouse coordinates may be absolute screen pixels or normalised to ``0..10000``
    with ``norm: true``. The console sends normalised values because it only knows
    the size of the frame it received, not the remote desktop resolution.
    """

    kind = event.get("kind")
    if kind == "mouse":
        x, y = _coordinates(event)
        moved = mouse_move(x, y)
        action = event.get("action", "move")
        if action == "move":
            return moved
        button = int(event.get("button", 1))
        if action == "click":
            pressed = mouse_button(button, True)
            return mouse_button(button, False) and pressed
        return mouse_button(button, action != "up")
    if kind == "wheel":
        return mouse_wheel(int(event.get("delta", 0)))
    if kind == "key":
        return key_event(int(event.get("code", 0)), bool(event.get("down", True)))
    if kind == "text":
        return type_text(str(event.get("text", "")))
    return False


def _coordinates(event: dict) -> tuple[int, int]:
    raw_x = int(event.get("x", 0))
    raw_y = int(event.get("y", 0))
    if not event.get("norm"):
        return raw_x, raw_y
    screen_w, screen_h = _screen_size()
    return (
        int(max(0.0, min(1.0, raw_x / 10000.0)) * max(0, screen_w - 1)),
        int(max(0.0, min(1.0, raw_y / 10000.0)) * max(0, screen_h - 1)),
    )


__all__ = [
    "EXTENDED_KEYS",
    "handle_event",
    "key_event",
    "mouse_button",
    "mouse_move",
    "mouse_wheel",
    "type_text",
]