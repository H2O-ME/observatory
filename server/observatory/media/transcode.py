"""图像转码：上传帧归一化（WebP）与缩略图生成。

两条铁律（0.2.10 的教训）：

1. **编码绝不在事件循环里跑**：一帧 WebP 编码 40–190ms，直接 ``await`` 会让整台
   服务器的请求处理停顿。这里统一走 ``asyncio.to_thread``。
2. **并发必须限流**：突发上传若不限流会把小 VPS 的 CPU 打满，长轮询和心跳一起饿死。
   用 ``Semaphore`` 卡住同时进行的编码数。

纯函数 ``normalise_frame`` / ``encode_thumbnail`` 与 IO 壳分离，方便单测（解码、缩放、
质量钳制都不需要事件循环）。
"""

from __future__ import annotations

import asyncio
import io
import logging

from PIL import Image

log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 2
DEFAULT_THUMB_WIDTH = 320
DEFAULT_THUMB_QUALITY = 68

_MIN_DIMENSION = 1
_MAX_THUMB_WIDTH = 1024


class ImageError(ValueError):
    """上传的字节不是可解码图像，或编码失败。"""


def _open(data: bytes) -> Image.Image:
    if not data:
        raise ImageError("空图像数据")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001 - Pillow 对坏数据抛的异常类型不统一，统一转成 ImageError
        raise ImageError(f"无法解码图像: {exc}") from exc
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return image


def _encode_webp(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=max(30, min(95, int(quality))), method=4)
    return buffer.getvalue()


def _resize_to_width(image: Image.Image, target_width: int) -> Image.Image:
    target = max(1, min(target_width, image.width))
    if target == image.width:
        return image
    height = max(_MIN_DIMENSION, round(image.height * target / image.width))
    return image.resize((target, height), Image.Resampling.BILINEAR)


def normalise_frame(data: bytes, max_width: int, quality: int) -> tuple[bytes, int, int]:
    """把任意输入图像重编码成可预测的 WebP，返回 ``(bytes, width, height)``。"""

    image = _open(data)
    width = max(1, int(max_width))
    if image.width > width:
        image = _resize_to_width(image, width)
    encoded = _encode_webp(image, quality)
    return encoded, image.width, image.height


def encode_thumbnail(data: bytes, width: int, quality: int = DEFAULT_THUMB_QUALITY) -> tuple[bytes, int, int]:
    """把（已归一的）帧压成缩略图，返回 ``(bytes, width, height)``。"""

    image = _open(data)
    target = max(48, min(_MAX_THUMB_WIDTH, int(width or DEFAULT_THUMB_WIDTH)))
    resized = _resize_to_width(image, target)
    encoded = _encode_webp(resized, quality)
    return encoded, resized.width, resized.height


def image_size(data: bytes) -> tuple[int, int]:
    """只解码取尺寸（归档取回时用于回填 frames.width/height）。"""

    image = _open(data)
    return image.width, image.height


class Thumbnailer:
    """``asyncio.to_thread`` + 信号量的转码执行器。"""

    def __init__(self, max_concurrent: int = DEFAULT_CONCURRENCY) -> None:
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))
        self.max_concurrent = max(1, int(max_concurrent))

    async def normalize(self, data: bytes, max_width: int, quality: int) -> tuple[bytes, int, int]:
        async with self._semaphore:
            return await asyncio.to_thread(normalise_frame, data, max_width, quality)

    async def thumbnail(
        self, data: bytes, width: int, quality: int = DEFAULT_THUMB_QUALITY
    ) -> tuple[bytes, int, int]:
        async with self._semaphore:
            return await asyncio.to_thread(encode_thumbnail, data, width, quality)


__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_THUMB_QUALITY",
    "DEFAULT_THUMB_WIDTH",
    "ImageError",
    "Thumbnailer",
    "encode_thumbnail",
    "image_size",
    "normalise_frame",
]