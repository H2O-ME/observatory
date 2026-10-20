"""Screen capture for the session this process runs in.

mss is created lazily per thread because its DXGI/GDI context is not thread safe.
The capture is downscaled and encoded to WebP in one pass; the change signature is
a 64x36 grayscale digest, which is cheap enough to run on every tick and good
enough to skip an unchanged desktop.
"""

from __future__ import annotations

import io
import logging
import threading

from PIL import Image

log = logging.getLogger(__name__)

_local = threading.local()


class CaptureError(RuntimeError):
    pass


def _engine():
    engine = getattr(_local, "engine", None)
    if engine is None:
        try:
            import mss

            engine = mss.mss()
        except Exception as exc:  # noqa: BLE001
            raise CaptureError(f"无法初始化屏幕采集: {exc}") from exc
        _local.engine = engine
    return engine


def monitors() -> list[dict]:
    try:
        return list(_engine().monitors)
    except Exception as exc:  # noqa: BLE001
        raise CaptureError(str(exc)) from exc


def grab(
    *,
    display: int = 0,
    max_width: int = 1600,
    quality: int = 72,
    signature: bool = True,
) -> tuple[bytes, int, int, int]:
    """Capture one frame.

    ``display`` 0 means all monitors combined, 1..N a specific monitor.
    Returns ``(webp_bytes, width, height, change_score)`` where ``change_score`` is
    the mean absolute difference against the previous signature (0..255).
    """

    engine = _engine()
    try:
        monitors_list = engine.monitors
        index = display if 0 <= display < len(monitors_list) else 1 if len(monitors_list) > 1 else 0
        shot = engine.grab(monitors_list[index])
    except Exception as exc:  # noqa: BLE001
        raise CaptureError(f"采集失败: {exc}") from exc

    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    width, height = image.size
    if max_width and width > max_width:
        new_height = max(1, round(height * max_width / width))
        image = image.resize((max_width, new_height), Image.Resampling.BILINEAR)
        width, height = image.size

    score = 0
    if signature:
        try:
            small = image.convert("L").resize((64, 36), Image.Resampling.BILINEAR)
            current = small.tobytes()
            previous = getattr(_local, "signature", None)
            if previous is not None and len(previous) == len(current):
                total = 0
                for left, right in zip(previous, current):
                    total += abs(left - right)
                score = int(total / len(current))
            _local.signature = current
        except Exception:  # noqa: BLE001
            score = 255

    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=max(20, min(95, quality)), method=4)
    return buffer.getvalue(), width, height, score


def reset_signature() -> None:
    _local.signature = None


__all__ = ["CaptureError", "grab", "monitors", "reset_signature"]