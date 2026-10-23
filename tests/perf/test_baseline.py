"""N4 性能回归：**同轮配对**（0.2.10 冻结纯函数 vs 0.3.0 media）。

口径（Lead/server-media 2026-10-24 裁决）：

* 绝对毫秒不可跨时段/跨机比较（同一台机器不同时段能差 2 倍，历史 390/312/55ms
  只是 0.2.10 在某一时段的记录）；
* N4 判据 = **当轮配对** `new / old` 的中位比 ≤ 1.10（耗时）、≤1.05（体积）、
  ≤1.20（RSS）；
* 0.2.10 转码实现已从产品代码删除，其纯函数冻结在
  ``tests/perf/legacy_transcode_0_2_10.py`` 作为同轮对照基准。
"""

from __future__ import annotations

import gc
import json
import statistics
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from observatory.media import transcode

from tests.perf import legacy_transcode_0_2_10 as legacy

pytestmark = pytest.mark.perf

REPO_ROOT = Path(__file__).resolve().parents[2]

TIME_TOLERANCE = 1.10  # 同轮配对 ≤ +10%
SIZE_TOLERANCE = 1.05  # 同轮配对 ≤ +5%
RSS_TOLERANCE = 1.20  # ≤ 1.2×

#: 0.2.10 记录值（只作为 RSS 的绝对参照 + 文档一致性；耗时/体积判据用配对）
RSS_BASELINE = {"idle_mb": 29.0, "delta_after_media_mb": 21.6}


def _synthetic_1600x900_jpeg() -> bytes:
    """与 tools/bench.py media 完全相同的确定性输入。"""

    base = Image.linear_gradient("L").resize((1600, 900))
    image = Image.merge("RGB", (base, base.rotate(90), base.rotate(180)))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def _timed(function):
    started = time.perf_counter()
    result = function()
    return result, (time.perf_counter() - started) * 1000.0


def _paired(old_call, new_call, runs: int = 12) -> tuple[list[float], list[float], list[float], list[int], list[int]]:
    """交替顺序跑 old/new，返回 (new_ms, old_ms, ratios, new_bytes, old_bytes)。"""

    new_costs: list[float] = []
    old_costs: list[float] = []
    ratios: list[float] = []
    new_sizes: list[int] = []
    old_sizes: list[int] = []
    for index in range(runs):
        if index % 2:
            old_result, old_cost = _timed(old_call)
            new_result, new_cost = _timed(new_call)
        else:
            new_result, new_cost = _timed(new_call)
            old_result, old_cost = _timed(old_call)
        new_costs.append(new_cost)
        old_costs.append(old_cost)
        ratios.append(new_cost / max(old_cost, 1e-6))
        new_sizes.append(len(new_result[0]))
        old_sizes.append(len(old_result[0]))
    return new_costs, old_costs, ratios, new_sizes, old_sizes


def test_n4_normalize_paired_not_regressed() -> None:
    """normalize：同轮配对 p50 ≤1.10×，输出体积 ≤1.05×。"""

    data = _synthetic_1600x900_jpeg()
    new_costs, old_costs, ratios, new_sizes, old_sizes = _paired(
        lambda: legacy.normalise_frame(data, 1600, 72),
        lambda: transcode.normalise_frame(data, 1600, 72),
    )
    assert statistics.median(ratios) <= TIME_TOLERANCE, (
        f"normalize 配对耗时退化: p50 new={statistics.median(new_costs):.1f}ms / "
        f"old={statistics.median(old_costs):.1f}ms = {statistics.median(ratios):.3f}x > 1.10x"
    )
    size_ratio = statistics.fmean(new_sizes) / max(1, statistics.fmean(old_sizes))
    assert size_ratio <= SIZE_TOLERANCE, f"normalize 输出体积退化: {size_ratio:.3f}x > 1.05x"


def test_n4_thumbnail_paired_not_regressed() -> None:
    """thumb320：在同一份归一化 WebP 上同轮配对 ≤1.10×、体积 ≤1.05×。"""

    data = _synthetic_1600x900_jpeg()
    normalised = transcode.normalise_frame(data, 1600, 72)[0]
    new_costs, old_costs, ratios, new_sizes, old_sizes = _paired(
        lambda: legacy.encode_thumbnail(normalised, 320),
        lambda: transcode.encode_thumbnail(normalised, 320, 68),
    )
    assert statistics.median(ratios) <= TIME_TOLERANCE, (
        f"thumbnail 配对耗时退化: p50 new={statistics.median(new_costs):.1f}ms / "
        f"old={statistics.median(old_costs):.1f}ms = {statistics.median(ratios):.3f}x > 1.10x"
    )
    size_ratio = statistics.fmean(new_sizes) / max(1, statistics.fmean(old_sizes))
    assert size_ratio <= SIZE_TOLERANCE, f"thumbnail 体积退化: {size_ratio:.3f}x > 1.05x"


def test_n4_bench_has_no_legacy_imports() -> None:
    """旧 `services/` 删除后 bench.py 不得再 import 它。"""

    source = (REPO_ROOT / "tools/bench.py").read_text(encoding="utf-8")
    assert "observatory.services" not in source
    assert "observatory_agent import capture" not in source
    assert "from observatory.media import transcode" in source


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
    """RSS：bench 子进程空闲 ≤1.2×29MB；media 20 帧增量 ≤1.2×21.6MB。"""

    media_report = _bench_json("media", 20)
    samples = {sample["name"]: sample for sample in media_report["samples"]}
    delta = samples.get("rss.delta_after_media")
    assert delta is not None, f"bench media 缺少 rss.delta_after_media；实际 {list(samples)}"
    delta_limit = RSS_BASELINE["delta_after_media_mb"] * RSS_TOLERANCE * 1024 * 1024
    assert delta["mean_bytes"] <= delta_limit, (
        f"media 连续转码 RSS 增量 {delta['mean_bytes'] / 1024 / 1024:.1f}MB > "
        f"{delta_limit / 1024 / 1024:.1f}MB（≤1.2×）"
    )

    frames_report = _bench_json("frames", 1)
    frames_samples = {sample["name"]: sample for sample in frames_report["samples"]}
    idle = frames_samples.get("rss.idle")
    assert idle is not None, "bench frames 需输出 rss.idle（0.2.10 采集删除后仍保留空闲基线探针）"
    idle_limit = RSS_BASELINE["idle_mb"] * RSS_TOLERANCE * 1024 * 1024
    assert idle["mean_bytes"] <= idle_limit, (
        f"空闲 RSS {idle['mean_bytes'] / 1024 / 1024:.1f}MB > {idle_limit / 1024 / 1024:.1f}MB（≤1.2×）"
    )


def test_n4_bench_comparison_table_is_well_formed() -> None:
    """bench media 必须输出 metric/new/baseline/ratio/verdict 四列语义。"""

    report = _bench_json("media", 3)
    comparisons = report.get("comparisons") or []
    assert comparisons, "bench media 缺少 comparisons 表"
    for item in comparisons:
        assert set(item) >= {"metric", "new", "baseline", "ratio", "tolerance", "verdict"}
        assert item["verdict"] in {"PASS", "FAIL"}
    gc.collect()