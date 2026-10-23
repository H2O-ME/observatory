#!/usr/bin/env python
"""N4 性能基线工具（0.3.0 新实现 vs 0.2.10 已记录基线）。

用法：
    python tools/bench.py media --runs 10     # 0.3.0 media 纯转码（任何平台可跑，N4 主入口）
    python tools/bench.py envelope --runs 2000  # 协议信封编解码吞吐
    python tools/bench.py frames --runs 10    # 0.2.10 整帧基线已随旧采集删除，仅报告基线数字
    python tools/bench.py all --json          # 机器可读输出

设计约束：
- 不 import 任何 0.2.10 旧模块（`services/`、旧 `agent capture` 已删除，W3）。
- N4 对照使用 `tests/perf/baseline.md` 里记录的 0.2.10 同机数字；新实现在本机测量，
  输出「metric / new / baseline / ratio / verdict」，判据：时间 ≤+10%、体积 ≤+5%、RSS ≤1.2×。
- `media` 用确定性合成图（不依赖桌面抓屏）；合成输入的 0.2.10 同输入基线由
  `tests/perf/baseline.md` 的 “0.3.0 media 合成输入对照” 一节固定。
"""

from __future__ import annotations

import argparse
import gc
import io
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# N4 判据与基线（0.2.10 同机实测，见 tests/perf/baseline.md）
# ---------------------------------------------------------------------------
TIME_TOLERANCE = 1.10
SIZE_TOLERANCE = 1.05
RSS_TOLERANCE = 1.20

#: 0.2.10 真实抓屏输入（保留为历史基线；新 0.3.0 采集由受控端负责，不在本工具重跑）
CAPTURED_BASELINE = {
    "frame.p50_ms": 390.24,
    "frame.bytes": 20_244,
    "frame.normalize_p50_ms": 312.16,
    "frame.normalize_bytes": 26_042,
    "frame.thumb_p50_ms": 55.43,
    "rss.idle_mb": 29.0,
    "rss.delta_after_frames_mb": 21.6,
}

#: 0.2.10 服务端转码在“合成图”输入上的同机实测（与 media 子命令同输入，判据直接可比）
SYNTHETIC_BASELINE = {
    "normalize.p50_ms": 417.71,
    "normalize.bytes": 11_532,
    "thumb.p50_ms": 78.45,
}


@dataclass
class Sample:
    name: str
    runs: int
    mean_ms: float
    p50_ms: float
    min_ms: float
    max_ms: float
    mean_bytes: int = 0
    note: str = ""


@dataclass
class Comparison:
    """N4 判据：new 相对 baseline 的比值与结论。"""

    metric: str
    new: float
    baseline: float
    tolerance: float
    unit: str = ""
    note: str = ""

    @property
    def ratio(self) -> float:
        return self.new / self.baseline if self.baseline else 0.0

    @property
    def verdict(self) -> str:
        return "PASS" if self.ratio <= self.tolerance else "FAIL"


@dataclass
class Report:
    machine: dict = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    comparisons: list[Comparison] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            "| 指标 | 运行次数 | 均值 ms | P50 ms | 最小 ms | 最大 ms | 平均字节 | 备注 |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for s in self.samples:
            lines.append(
                f"| {s.name} | {s.runs} | {s.mean_ms:.2f} | {s.p50_ms:.2f} | {s.min_ms:.2f} | "
                f"{s.max_ms:.2f} | {s.mean_bytes or '—'} | {s.note} |"
            )
        if self.comparisons:
            lines += [
                "",
                "| 指标 | 新实现 | 0.2.10 基线 | 比值 | 判据 | 结论 |",
                "|---|---:|---:|---:|---|---|",
            ]
            for c in self.comparisons:
                unit = c.unit or ""
                note = f"（{c.note}）" if c.note else ""
                lines.append(
                    f"| {c.metric} | {c.new:.2f}{unit} | {c.baseline:.2f}{unit} | "
                    f"{c.ratio:.3f}x | ≤{c.tolerance:.2f}x | **{c.verdict}**{note} |"
                )
        if self.unavailable:
            lines.append("")
            lines.append("不可用：" + "；".join(self.unavailable))
        return "\n".join(lines)

    def as_json(self) -> dict:
        payload = {
            "machine": self.machine,
            "samples": [asdict(s) for s in self.samples],
            "comparisons": [
                {
                    "metric": c.metric,
                    "new": c.new,
                    "baseline": c.baseline,
                    "ratio": round(c.ratio, 4),
                    "tolerance": c.tolerance,
                    "verdict": c.verdict,
                    "note": c.note,
                }
                for c in self.comparisons
            ],
            "unavailable": self.unavailable,
        }
        return payload


def _machine() -> dict:
    try:
        import platform

        return {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor(),
        }
    except Exception:  # noqa: BLE001
        return {}


def _rss_bytes() -> int | None:
    try:
        import psutil  # type: ignore

        return int(psutil.Process().memory_info().rss)
    except Exception:  # noqa: BLE001
        return None


def _synthetic_1600x900_jpeg(quality: int = 80) -> bytes:
    """确定性合成图（渐变三通道），避免 N4 依赖桌面抓屏/远程会话。"""

    from PIL import Image  # noqa: PLC0415

    base = Image.linear_gradient("L").resize((1600, 900))
    red = base
    green = base.rotate(90)
    blue = base.rotate(180)
    image = Image.merge("RGB", (red, green, blue))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def bench_frames(runs: int, report: Report) -> None:
    """0.2.10 整帧路径已随旧采集实现删除（W3）；这里只报告历史基线，退出码仍 0。"""

    report.unavailable.append(
        "0.2.10 受控端采集实现已删除；整帧基线 "
        f"{CAPTURED_BASELINE['frame.p50_ms']:.2f}ms / {CAPTURED_BASELINE['frame.bytes']}B、"
        f"服务端归一化 {CAPTURED_BASELINE['frame.normalize_p50_ms']:.2f}ms / "
        f"{CAPTURED_BASELINE['frame.normalize_bytes']}B、thumb320 "
        f"{CAPTURED_BASELINE['frame.thumb_p50_ms']:.2f}ms、"
        f"RSS {CAPTURED_BASELINE['rss.idle_mb']:.0f}→"
        f"{CAPTURED_BASELINE['rss.idle_mb'] + CAPTURED_BASELINE['rss.delta_after_frames_mb']:.0f}MB "
        "见 tests/perf/baseline.md；新 0.3.0 采集/整帧由受控端测试覆盖"
    )
    gc.collect()
    idle = _rss_bytes()
    if idle is not None:
        report.samples.append(
            Sample(
                name="rss.idle",
                runs=1,
                mean_ms=0.0,
                p50_ms=0.0,
                min_ms=0.0,
                max_ms=0.0,
                mean_bytes=idle,
                note=f"{idle // 1024 // 1024}MB",
            )
        )


def bench_media(runs: int, report: Report) -> None:
    """0.3.0 media 纯转码路径（N4 主入口）。

    ``normalise_frame`` / ``encode_thumbnail`` 是同步函数（线上包在
    ``asyncio.to_thread`` 里），所以这里直接同步调用，不引入事件循环。
    新实现只测一次，基线取 baseline.md 中 0.2.10 在**同一合成输入**上的数字。
    """

    sys.path.insert(0, str(REPO_ROOT / "server"))
    try:
        from observatory.media import transcode  # type: ignore
    except Exception as exc:  # noqa: BLE001
        report.unavailable.append(f"0.3.0 media transcode unavailable: {exc}")
        return

    data = _synthetic_1600x900_jpeg()
    gc.collect()
    rss_before = _rss_bytes()
    norm_costs: list[float] = []
    thumb_costs: list[float] = []
    norm_sizes: list[int] = []
    thumb_sizes: list[int] = []
    for _ in range(runs):
        started = time.perf_counter()
        normalised, _width, _height = transcode.normalise_frame(data, 1600, 72)
        norm_costs.append((time.perf_counter() - started) * 1000.0)
        norm_sizes.append(len(normalised))
        started = time.perf_counter()
        thumb, _thumb_w, _thumb_h = transcode.encode_thumbnail(normalised, 320, 68)
        thumb_costs.append((time.perf_counter() - started) * 1000.0)
        thumb_sizes.append(len(thumb))
    gc.collect()
    rss_after = _rss_bytes()
    if rss_before is not None and rss_after is not None:
        report.samples.append(
            Sample(
                name="rss.delta_after_media",
                runs=runs,
                mean_ms=0.0,
                p50_ms=0.0,
                min_ms=0.0,
                max_ms=0.0,
                mean_bytes=max(0, rss_after - rss_before),
                note=f"RSS {rss_before // 1024 // 1024}MB → {rss_after // 1024 // 1024}MB",
            )
        )

    report.samples.append(
        Sample(
            name="server.normalize_webp_q72 (0.3.0 media)",
            runs=runs,
            mean_ms=statistics.fmean(norm_costs),
            p50_ms=statistics.median(norm_costs),
            min_ms=min(norm_costs),
            max_ms=max(norm_costs),
            mean_bytes=int(statistics.fmean(norm_sizes)),
            note="media/transcode.normalise_frame(1600, q72)，输入 1600x900 合成 JPEG",
        )
    )
    report.samples.append(
        Sample(
            name="server.thumbnail_320_q68 (0.3.0 media)",
            runs=runs,
            mean_ms=statistics.fmean(thumb_costs),
            p50_ms=statistics.median(thumb_costs),
            min_ms=min(thumb_costs),
            max_ms=max(thumb_costs),
            mean_bytes=int(statistics.fmean(thumb_sizes)),
            note="media/transcode.encode_thumbnail(320, q68)",
        )
    )

    norm_p50 = statistics.median(norm_costs)
    thumb_p50 = statistics.median(thumb_costs)
    norm_bytes = int(statistics.fmean(norm_sizes))
    report.comparisons += [
        Comparison(
            "server.normalize p50",
            norm_p50,
            SYNTHETIC_BASELINE["normalize.p50_ms"],
            TIME_TOLERANCE,
            "ms",
            "同合成输入",
        ),
        Comparison(
            "server.normalize bytes",
            norm_bytes,
            SYNTHETIC_BASELINE["normalize.bytes"],
            SIZE_TOLERANCE,
            "B",
            "同合成输入",
        ),
        Comparison(
            "server.thumbnail320 p50",
            thumb_p50,
            SYNTHETIC_BASELINE["thumb.p50_ms"],
            TIME_TOLERANCE,
            "ms",
            "同合成输入",
        ),
    ]


def bench_envelope(runs: int, report: Report) -> None:
    """v3 信封编解码吞吐。"""

    try:
        from observatory.link.envelope import decode, encode  # type: ignore
    except Exception as exc:  # noqa: BLE001
        report.unavailable.append(f"v3 envelope not available: {exc}")
        return

    payload = {"agent_version": "0.3.0", "state": {"transport": "ws"}}
    costs: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        blob = encode("heartbeat", payload)
        assert decode(blob)
        costs.append((time.perf_counter() - started) * 1000.0)
    report.samples.append(
        Sample(
            name="envelope.roundtrip (v3)",
            runs=runs,
            mean_ms=statistics.fmean(costs),
            p50_ms=statistics.median(costs),
            min_ms=min(costs),
            max_ms=max(costs),
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="N4 performance baseline")
    parser.add_argument("command", choices=["frames", "media", "envelope", "all"])
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = Report(machine=_machine())
    if args.command in {"frames", "all"}:
        bench_frames(args.runs, report)
    if args.command in {"media", "all"}:
        bench_media(args.runs, report)
    if args.command in {"envelope", "all"}:
        bench_envelope(max(200, args.runs * 20), report)

    if args.json:
        print(json.dumps(report.as_json(), ensure_ascii=False, indent=2))
    else:
        print(report.to_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))