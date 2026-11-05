"""0.3.17：控制帧必须有独立的（更小的）分辨率上限。

为什么值得单独测：控制流在 1600 宽 JPEG（150-250KB/帧）下会把 1-2Mbps 的
上行塞死——合并发送让它不断流，但每帧在 TCP 上队头阻塞输入事件，"点击无
响应"就是这么来的。这条链路的关键契约是：**控制帧的编码宽度只跟
``control_max_width`` 走，与周期采集的 ``max_width`` 互不影响**——否则改时间
线画质会悄悄改变实时流体积，反之亦然。用假 source 出 1920×1080 图，不碰真桌面。
"""

from __future__ import annotations

import threading

import pytest
from PIL import Image

from observatory_agent.tasks.capture import (
    DEFAULT_CONTROL_MAX_WIDTH,
    DEFAULT_CONTROL_QUALITY,
    CaptureEngine,
    CaptureSettings,
)

pytestmark = pytest.mark.unit

_BIG = (1920, 1080)


class _FakeSource:
    """返回固定 1920×1080 渐变图的假抓屏源（FrameSource 协议的最小子集）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        image = Image.new("RGB", _BIG)
        for x in range(0, _BIG[0], 64):
            for y in range(0, _BIG[1], 64):
                image.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
        self._image = image

    def grab(self, _display: int) -> Image.Image:
        with self.lock:
            return self._image.copy()

    def monitors(self) -> list[dict[str, int]]:
        return [{"index": 0, "width": _BIG[0], "height": _BIG[1]}]


def _engine(**settings: object) -> CaptureEngine:
    return CaptureEngine(source=_FakeSource(), settings=dict(settings), clock=lambda: 1_700_000_000_000)


def test_control_defaults_are_smaller_than_timeline() -> None:
    resolved = CaptureSettings.from_mapping({})
    assert resolved.control_max_width == DEFAULT_CONTROL_MAX_WIDTH == 1024
    assert resolved.control_quality == DEFAULT_CONTROL_QUALITY == 40
    # 控制流上限必须真的比时间线小，否则这次修复没有意义
    assert resolved.control_max_width < resolved.max_width


def test_control_frame_is_encoded_at_control_max_width() -> None:
    engine = _engine()  # 默认：max_width=1600 / control_max_width=1024
    frame = engine.capture_control_frame()
    assert frame.width == 1024, "控制帧宽度必须走 control_max_width，而不是 max_width"
    assert frame.kind == "control"
    assert 1600 < 1920, "保证假源确实大于两个上限（测试有效性前提）"


def test_control_max_width_is_independent_from_timeline_max_width() -> None:
    engine = _engine(max_width=1600, control_max_width=640)
    control = engine.capture_control_frame()
    assert control.width == 640
    timeline = engine.capture_now()
    assert timeline.width == 1600, "改控制流上限不得影响时间线出图"


def test_control_max_width_clamps_and_survives_bad_values() -> None:
    assert CaptureSettings.from_mapping({"control_max_width": 100}).control_max_width == 320
    assert CaptureSettings.from_mapping({"control_max_width": 99999}).control_max_width == 3840
    assert CaptureSettings.from_mapping({"control_max_width": "abc"}).control_max_width == 1024


def test_agent_fallback_settings_carry_the_new_keys() -> None:
    from observatory_agent.core.config import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["control_max_width"] == 1024
    assert DEFAULT_SETTINGS["control_quality"] == 40


def test_control_frame_bytes_shrink_beyond_ratio() -> None:
    """同源同质下，1024 宽控制帧的体积应显著小于 1600 宽（像素比 0.41）。"""

    engine = _engine(control_quality=55)
    small = engine.capture_control_frame().payload
    engine.apply_settings({"control_max_width": 1600, "control_quality": 55})
    big = engine.capture_control_frame().payload
    assert len(small) < len(big) * 0.6, f"降宽后体积没有按比例下降: {len(small)} vs {len(big)}"
