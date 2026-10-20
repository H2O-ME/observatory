"""Thumbnail generation.

Run in a small worker pool: encoding a WebP is CPU-bound and blocking the event
loop for 40ms per frame is what made the timeline crawl. Concurrency is capped so
a burst of uploads cannot starve request handling on a small VPS.
"""

from __future__ import annotations

import asyncio
import io
import logging

from PIL import Image

log = logging.getLogger(__name__)

MAX_CONCURRENT = 2
THUMB_QUALITY = 68


class ImageError(ValueError):
    pass


def _encode_thumbnail(data: bytes, width: int) -> tuple[bytes, int, int]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            target = max(48, min(640, width))
            if image.width > target:
                height = max(1, round(image.height * target / image.width))
                resized = image.resize((target, height), Image.Resampling.BILINEAR)
            else:
                resized = image.copy()
            buffer = io.BytesIO()
            resized.save(buffer, format="WEBP", quality=THUMB_QUALITY, method=4)
            return buffer.getvalue(), resized.width, resized.height
    except Exception as exc:  # noqa: BLE001 - any decoder failure is the same to us
        raise ImageError(f"无法解码图像: {exc}") from exc


def _normalise_frame(data: bytes, max_width: int, quality: int) -> tuple[bytes, int, int]:
    """Re-encode an uploaded frame to a predictable WebP."""

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        width, height = image.size
        if width > max_width:
            height = max(1, round(height * max_width / width))
            image = image.resize((max_width, height), Image.Resampling.BILINEAR)
            width = max_width
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=max(30, min(95, quality)), method=4)
        return buffer.getvalue(), width, height


class Thumbnailer:
    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def thumbnail(self, data: bytes, width: int) -> tuple[bytes, int, int]:
        async with self._semaphore:
            return await asyncio.to_thread(_encode_thumbnail, data, width)

    async def normalize(self, data: bytes, max_width: int, quality: int) -> tuple[bytes, int, int]:
        async with self._semaphore:
            return await asyncio.to_thread(_normalise_frame, data, max_width, quality)


__all__ = ["ImageError", "MAX_CONCURRENT", "Thumbnailer"]