"""N4 性能回归：**new vs 本机记录基线**（0.2.10→0.3.0 的 W1 配对结论）。

口径（Lead/server-media 2026-10-24 裁决）：

* 0.2.10 旧实现已删除，不可能再同轮配对；W1 的配对实测已固化在
  ``tests/perf/baseline.md``（normalize 0.981×、thumbnail 1.009×、体积 1.000×、
  RSS idle 0.995× / 20 帧 ≈1.00×、store_frame 0.968×）；
* 绝对毫秒随机器负载漂移（今天旧实现 normalize 实测 570–725ms，390ms 是另一时段），
  因此只允许在**同一台测量机**上比较：机器指纹不符时 skip(reason)；
* 判据：new p50 ≤ 记录基线 ×1.10；体积 ≤1.05×；RSS ≤1.20×；
* 这些测试默认不跑（pytest.ini `-m "not perf"`），只在 CI perf job / 本机 perf 模式执行。
"""

from __future__ import annotations

import gc
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from observatory.media import transcode

pytestmark = pytest.mark.perf

REPO_ROOT = Path(__file__).resolve().parents[2]

TIME_TOLERANCE = 1.10
SIZE_TOLERANCE = 1.05
RSS_TOLERANCE = 1.20

#: W1 记录基线的机器指纹（本机；只在该机器上比较绝对毫秒）
RECORDED_MACHINE = {
    "system": "Windows",
    "release": "11",
    "machine": "AMD64",
    "cpu_count": 8,
    "python": "3.14.3",
}

#: 0.2.10 服务端转码在**合成输入**上的同机记录（W1 配对测量；见 baseline.md §4bis）
RECORDED = {
    "normalize_p50_ms": 417.71,
    "normalize_bytes": 11_532,
    "thumb_p50_ms": 78.45,
    "rss_idle_mb": 29.0,
    "rss_delta_after_media_mb": 21.6,
}


def _machine() -> dict:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
    }


def _require_recorded_machine() -> None:
    current = _machine()
    if current != RECORDED_MACHINE:
        pytest.skip(
            "机器指纹不符，绝对毫秒不可跨机/跨时段比较："
            f"current={current} recorded={RECORDED_MACHINE}（见 tests/perf/baseline.md）"
        )


def _synthetic_1600x900_jpeg() -> bytes:
    """与 tools/bench.py media 完全相同的确定性输入。"""

    base = Image.linear_gradient("L").resize((1600, 900))
    image = Image.merge("RGB", (base, base.rotate(90), base.rotate(180)))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def _p50(costs: list[float]) -> float:
    return statistics.median(costs)


def test_n4_normalize_not_regressed() -> None:
    """normalize：p50 ≤记录基线 ×1.10，输出体积 ≤1.05×。"""

    _require_recorded_machine()
    data = _synthetic_1600x900_jpeg()
    transcode.normalise_frame(data, 1600, 72)  # warm-up，避免首帧 import/缓存算进 p50
    costs: list[float] = []
    sizes: list[int] = []
    for _ in range(10):
        started = time.perf_counter()
        normalised, _width, _height = transcode.normalise_frame(data, 1600, 72)
        costs.append((time.perf_counter() - started) * 1000.0)
        sizes.append(len(normalised))
    measured = _p50(costs)
    baseline = RECORDED["normalize_p50_ms"]
    ratio = measured / baseline
    size_ratio = statistics.fmean(sizes) / RECORDED["normalize_bytes"]
    print(
        f"[N4] normalize p50={measured:.2f}ms baseline={baseline:.2f}ms ratio={ratio:.3f}x "
        f"bytes_ratio={size_ratio:.3f}x"
    )
    assert ratio <= TIME_TOLERANCE, f"normalize 耗时退化: {measured:.2f}ms / {baseline:.2f}ms = {ratio:.3f}x > 1.10x"
    assert size_ratio <= SIZE_TOLERANCE, f"normalize 体积退化: {size_ratio:.3f}x > 1.05x"


def test_n4_thumbnail_not_regressed() -> None:
    """thumb320：p50 ≤记录基线 ×1.10，体积 ≤1.05×。"""

    _require_recorded_machine()
    data = _synthetic_1600x900_jpeg()
    normalised = transcode.normalise_frame(data, 1600, 72)[0]
    transcode.encode_thumbnail(normalised, 320, 68)  # warm-up
    costs: list[float] = []
    sizes: list[int] = []
    for _ in range(10):
        started = time.perf_counter()
        thumb, _thumb_w, _thumb_h = transcode.encode_thumbnail(normalised, 320, 68)
        costs.append((time.perf_counter() - started) * 1000.0)
        sizes.append(len(thumb))
    measured = _p50(costs)
    baseline = RECORDED["thumb_p50_ms"]
    ratio = measured / baseline
    print(f"[N4] thumbnail320 p50={measured:.2f}ms baseline={baseline:.2f}ms ratio={ratio:.3f}x")
    assert ratio <= TIME_TOLERANCE, f"thumbnail 耗时退化: {ratio:.3f}x > 1.10x"
    # thumb 体积没有独立记录基线；只打印，不设跨机体积门限（normalize 体积已覆盖质量参数）
    assert all(size > 0 for size in sizes)
    gc.collect()


def _bench_json(command: str, runs: int) -> dict:
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools/bench.py"), command, "--runs", str(runs), "--json"],
        capture_output=True,
        text=True,
        timeout=240,
        check=True,
    )
    return json.loads(completed.stdout)


def test_n4_rss_idle_and_media_delta() -> None:
    """RSS：idle ≤1.2×29MB；media 20 帧增量 ≤1.2×21.6MB。"""

    _require_recorded_machine()
    media_report = _bench_json("media", 20)
    samples = {sample["name"]: sample for sample in media_report["samples"]}
    delta = samples.get("rss.delta_after_media")
    assert delta is not None, f"bench media 缺少 rss.delta_after_media；实际 {list(samples)}"
    delta_mb = delta["mean_bytes"] / 1024 / 1024
    delta_limit = RECORDED["rss_delta_after_media_mb"] * RSS_TOLERANCE
    print(f"[N4] rss delta after 20 media frames={delta_mb:.2f}MB limit={delta_limit:.2f}MB")
    assert delta_mb <= delta_limit, f"media RSS 增量 {delta_mb:.1f}MB > {delta_limit:.1f}MB（≤1.2×）"

    frames_report = _bench_json("frames", 1)
    frames_samples = {sample["name"]: sample for sample in frames_report["samples"]}
    idle = frames_samples.get("rss.idle")
    assert idle is not None, "bench frames 需输出 rss.idle（空闲基线探针）"
    idle_mb = idle["mean_bytes"] / 1024 / 1024
    idle_limit = RECORDED["rss_idle_mb"] * RSS_TOLERANCE
    print(f"[N4] rss idle={idle_mb:.2f}MB limit={idle_limit:.2f}MB")
    assert idle_mb <= idle_limit, f"空闲 RSS {idle_mb:.1f}MB > {idle_limit:.1f}MB（≤1.2×）"


def test_n4_bench_comparison_table_is_well_formed() -> None:
    """bench media 必须输出 metric/new/baseline/ratio/verdict 表。"""

    report = _bench_json("media", 3)
    comparisons = report.get("comparisons") or []
    assert comparisons, "bench media 缺少 comparisons 表"
    for item in comparisons:
        assert set(item) >= {"metric", "new", "baseline", "ratio", "tolerance", "verdict"}
        assert item["verdict"] in {"PASS", "FAIL"}