"""多显示器 / DPI 坐标换算（A16）：纯函数，误差 ≤2px。W2 起启用。

关键基准（0.3.0 的正确语义，勿再按主屏算）：
    归一化坐标的基准是**整个虚拟桌面帧**（受控端 `mss.monitors[0]` 抓的是所有显示器
    拼合图），不是主显示器。两屏 1920 + 1920 时帧宽 3840，左屏 x 范围 -1920..0。
    换算用 `width - 1` 作为跨度（0→left，1→right-1），与 MOUSEEVENTF_VIRTUALDESK
    的闭区间一致，误差 ≤1px；dpi_scale 不参与换算（PerMonitorV2 下已是物理像素，
    再乘一次就是 0.2.10 的双重缩放 bug）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _two_screens():
    from observatory_agent.tasks.input import VirtualDesktop  # noqa: PLC0415

    return VirtualDesktop(
        monitors=[
            # 副屏在左侧且负坐标；主屏 1920x1080
            {"left": -1920, "top": 0, "width": 1920, "height": 1080, "primary": False},
            {"left": 0, "top": 0, "width": 1920, "height": 1080, "primary": True},
        ]
    )


@pytest.mark.skip(reason="W2: 等待 agent/observatory_agent/tasks/input.py 的纯函数坐标层")
def test_negative_origin_second_monitor() -> None:
    from observatory_agent.tasks.input import to_absolute  # noqa: PLC0415

    desktop = _two_screens()
    # 帧宽 = 3840（左屏 -1920..0 + 右屏 0..1920），不是主屏 1920。
    # x = -1920 + round(0.25 × (3840-1)) = -1920 + 960 = -960
    point = to_absolute(desktop, 0.25, 0.5, dpi_scale=1.0)
    assert point.x == pytest.approx(-960, abs=2)
    assert point.y == pytest.approx(540, abs=2)
    # 帧边界必须落在虚拟桌面的角上（0→left、1→right-1）
    assert to_absolute(desktop, 0.0, 0.0).x == -1920
    assert to_absolute(desktop, 1.0, 1.0).x == 1919


@pytest.mark.skip(reason="W2: 等待 tasks/input.py 的 DPI 感知缩放")
def test_four_monitors_negative_origin_dpi_150() -> None:
    """四屏（左右 + 下 + 更高副屏）+ 负坐标 + 150% DPI：换算仍是物理像素。"""

    from observatory_agent.tasks.input import VirtualDesktop, to_absolute  # noqa: PLC0415

    desktop = VirtualDesktop(
        monitors=[
            {"left": -1920, "top": 0, "width": 1920, "height": 1080},          # 左上
            {"left": 0, "top": 0, "width": 1920, "height": 1080, "primary": True},  # 主屏
            {"left": 1920, "top": 0, "width": 2560, "height": 1440},          # 右侧竖屏
            {"left": -1920, "top": 1080, "width": 1920, "height": 1200},      # 左下
        ]
    )
    # 虚拟桌面：left=-1920, top=0, width=6400, height=2280
    assert (desktop.left, desktop.top, desktop.width, desktop.height) == (-1920, 0, 6400, 2280)
    # x = -1920 + round(0.5 × 6399) = -1920 + 3200 = 1280
    # y = 0 + round(0.5 × 2279) = 1140
    point = to_absolute(desktop, 0.5, 0.5, dpi_scale=1.5)
    assert point.x == pytest.approx(1280, abs=2)
    assert point.y == pytest.approx(1140, abs=2)
    # 150% DPI 不得二次缩放：与 100% 结果完全一致
    reference = to_absolute(desktop, 0.5, 0.5, dpi_scale=1.0)
    assert (point.x, point.y) == (reference.x, reference.y)
    # 四屏边界
    assert (to_absolute(desktop, 0.0, 0.0).x, to_absolute(desktop, 0.0, 0.0).y) == (-1920, 0)
    assert (to_absolute(desktop, 1.0, 1.0).x, to_absolute(desktop, 1.0, 1.0).y) == (4479, 2279)


@pytest.mark.skip(reason="W2: 等待 tasks/input.py 的 DPI 感知缩放")
def test_dpi_150_percent_error_within_2px() -> None:
    from observatory_agent.tasks.input import VirtualDesktop, to_absolute  # noqa: PLC0415

    desktop = VirtualDesktop(
        monitors=[{"left": 0, "top": 0, "width": 1920, "height": 1080, "primary": True}]
    )
    point = to_absolute(desktop, 0.5, 0.5, dpi_scale=1.5)
    # 单屏：round(0.5×1919)=960，round(0.5×1079)=540；150% 不许变成 1440/810
    assert point.x == 960
    assert point.y == 540


@pytest.mark.skip(reason="W2: 归一化坐标 (0..10000) 与 SendInput 绝对坐标互转")
def test_normalized_roundtrip_is_lossless_enough() -> None:
    from observatory_agent.tasks.input import from_normalized, to_normalized  # noqa: PLC0415

    for normalized in (0, 1, 5_000, 9_999, 10_000):
        assert from_normalized(to_normalized(normalized)) == pytest.approx(normalized, abs=1)