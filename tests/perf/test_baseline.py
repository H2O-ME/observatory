"""N4 性能回归（W3 起启用；baseline 见同目录 baseline.md）。

判据（spec §3.2 已裁决）：**同机前后相对比较**，不做跨机绝对比较。
- 帧处理耗时 ≤ +10%
- 帧体积 ≤ +5%
- 空闲 RSS ≤ 1.2×（连续采集后 RSS 增量 ≤ 1.2× 基线增量）

下面 BASELINE 里的绝对数字是「本机 0.2.10 实测」（390ms/312ms/55ms/29MB），
只用于同机回归的比例计算；历史文档里的 76ms 是另一台物理机，不作为门限。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.perf

REPO_ROOT = Path(__file__).resolve().parents[2]

TIME_TOLERANCE = 1.10  # ≤ +10%
SIZE_TOLERANCE = 1.05  # ≤ +5%
RSS_TOLERANCE = 1.20  # ≤ 1.2×

#: 本机 0.2.10 实测值（同机回归的参照点，不是跨机门限）
BASELINE = {
    "frame.grab+webp_q72 (0.2.10, warm)": {"p50_ms": 390.24, "bytes": 20_244},
    "server.normalize_webp_q72 (0.2.10)": {"p50_ms": 312.16, "bytes": 26_042},
    "server.thumbnail_320_q68 (0.2.10)": {"p50_ms": 55.43, "bytes": 0},
}

#: 本机 0.2.10 内存基线（见 baseline.md §3）
RSS_BASELINE = {
    "idle_mb": 29.0,
    "delta_after_frames_mb": 21.6,
}


@pytest.mark.skip(reason="W3: 新实现就位后跑 tools/bench.py 与 baseline 对比")
def test_n4_frame_cost_and_size_not_regressed() -> None:
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools/bench.py"), "frames", "--runs", "8", "--json"],
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    report = json.loads(completed.stdout)
    samples = {sample["name"]: sample for sample in report["samples"]}
    for name, baseline in BASELINE.items():
        assert name in samples, f"bench 缺少指标 {name}；实际: {list(samples)}"
        sample = samples[name]
        time_limit = baseline["p50_ms"] * TIME_TOLERANCE
        assert sample["p50_ms"] <= time_limit, (
            f"{name} 耗时退化: p50={sample['p50_ms']:.1f}ms > {time_limit:.1f}ms（≤+10%）"
        )
        if baseline["bytes"]:
            size_limit = baseline["bytes"] * SIZE_TOLERANCE
            assert sample["mean_bytes"] <= size_limit, (
                f"{name} 体积退化: {sample['mean_bytes']}B > {size_limit:.0f}B（≤+5%）"
            )


@pytest.mark.skip(reason="W3: 空闲内存对比需要新受控端可执行产物")
def test_n4_idle_memory_not_regressed() -> None:
    """空闲 RSS ≤1.2× 本机 0.2.10 基线；连续采集后增量 ≤1.2× 基线增量。"""

    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools/bench.py"), "frames", "--runs", "8", "--json"],
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    report = json.loads(completed.stdout)
    samples = {sample["name"]: sample for sample in report["samples"]}
    idle = samples.get("rss.idle")
    assert idle is not None, "bench 需要输出 rss.idle 指标（W3 补）"
    idle_limit = RSS_BASELINE["idle_mb"] * RSS_TOLERANCE
    assert idle["mean_bytes"] <= idle_limit * 1024 * 1024, (
        f"空闲 RSS 退化: {idle['mean_bytes'] / 1024 / 1024:.1f}MB > {idle_limit:.1f}MB（≤1.2×）"
    )
    delta = samples.get("rss.delta_after_frames")
    assert delta is not None, "bench 需要输出 rss.delta_after_frames 指标"
    delta_limit = RSS_BASELINE["delta_after_frames_mb"] * RSS_TOLERANCE
    assert delta["mean_bytes"] <= delta_limit * 1024 * 1024, (
        f"连续采集 RSS 增量退化: {delta['mean_bytes'] / 1024 / 1024:.1f}MB > {delta_limit:.1f}MB（≤1.2×）"
    )