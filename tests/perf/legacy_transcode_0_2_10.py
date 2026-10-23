"""冻结的 0.2.10 服务端转码实现（N4 同轮配对基准，**不要修改**）。

产品代码里的 `observatory/services/transcode.py` 会在 W3 删除；N4 判据要求
"当轮配对 old × 容差"，因此这里保留 0.2.10 的两个纯函数作为不可变证据
（与 `tests/contract/fixtures/agent_0_2_10.py` 同一策略）。

只有测试可以 import 本模块；生产代码不得引用。
"""

from __future__ import annotations

import io

from PIL import Image

THUMB_QUALITY = 68


class ImageError(ValueError):
    pass


def encode_thumbnail(data: bytes, width: int) -> tuple[bytes, int, int]:
    """0.2.10 `_encode_thumbnail`：thumb 宽 320、q68、method=4、BILINEAR。"""

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
    except Exception as exc:  # noqa: BLE001 - 与 0.2.10 行为一致
        raise ImageError(f"无法解码图像: {exc}") from exc


def normalise_frame(data: bytes, max_width: int, quality: int) -> tuple[bytes, int, int]:
    """0.2.10 `_normalise_frame`：超宽才缩放、WebP q=clamp(30..95)、method=4。"""

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


__all__ = ["ImageError", "THUMB_QUALITY", "encode_thumbnail", "normalise_frame"]