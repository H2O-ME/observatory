#!/usr/bin/env python
"""N4 性能基线工具（0.2.10 → 0.3.0 对照）。

用法：
    python tools/bench.py frames --runs 10            # 受控端整帧成本（PIL/mss，Windows）
    python tools/bench.py envelope --runs 2000        # 协议信封编解码吞吐（W1 起）
    python tools/bench.py all --json                   # 机器可读输出

设计约束：
- 不依赖 pytest；可被 `tests/perf/` 与 CI（workflow_dispatch / perf job）直接调用。
- 输出可直接贴进 `tests/perf/baseline.md` 的 markdown 表。
- 0.2.10 采集实现删除后，`frames` 自动退化为"不可用"并以退出码 0 返回，
  基线数字留在 baseline.md 与 tests/perf/test_baseline.py 的阈值表里。
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


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
class Report:
    machine: dict = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
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
        if self.unavailable:
            lines.append("")
            lines.append("不可用：" + "；".join(self.unavailable))
        return "\n".join(lines)


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


def bench_frames(runs: int, report: Report) -> None:
    """受控端整帧：mss 抓屏 → 缩放 → WebP q72（与 capture.grab 同参数）。"""

    sys.path.insert(0, str(REPO_ROOT / "agent"))
    try:
        from observatory_agent import capture  # type: ignore
    except Exception as exc:  # noqa: BLE001
        report.unavailable.append(f"legacy capture unavailable: {exc}")
        return

    gc.collect()
    before = _rss_bytes()
    costs: list[float] = []
    sizes: list[int] = []
    for index in range(runs):
        started = time.perf_counter()
        data, width, height, _score = capture.grab(max_width=1600, quality=72)
        costs.append((time.perf_counter() - started) * 1000.0)
        sizes.append(len(data))
        if index == 0:
            report.samples.append(
                Sample(
                    name="frame.grab+webp_q72 (0.2.10)",
                    runs=1,
                    mean_ms=costs[0],
                    p50_ms=costs[0],
                    min_ms=costs[0],
                    max_ms=costs[0],
                    mean_bytes=len(data),
                    note=f"{width}x{height}",
                )
            )
    after = _rss_bytes()
    if before is not None and after is not None:
        report.samples.append(
            Sample(
                name="rss.delta_after_frames",
                runs=runs,
                mean_ms=0.0,
                p50_ms=0.0,
                min_ms=0.0,
                max_ms=0.0,
                mean_bytes=after - before,
                note=f"RSS {before // 1024 // 1024}MB → {after // 1024 // 1024}MB",
            )
        )
    report.samples.append(
        Sample(
            name="frame.grab+webp_q72 (0.2.10, warm)",
            runs=runs,
            mean_ms=statistics.fmean(costs),
            p50_ms=statistics.median(costs),
            min_ms=min(costs),
            max_ms=max(costs),
            mean_bytes=int(statistics.fmean(sizes)),
        )
    )

    # 服务端 0.2.10：normalize(q72) + thumbnail(320/q68)，参数取自 services/ingest.py:41-46
    sys.path.insert(0, str(REPO_ROOT / "server"))
    try:
        from observatory.services import transcode  # type: ignore

        sample = capture.grab(max_width=1600, quality=72)[0]
        server_costs: list[float] = []
        thumb_costs: list[float] = []
        norm_sizes: list[int] = []
        for _ in range(runs):
            started = time.perf_counter()
            norm, _w, _h = transcode._normalise_frame(sample, 1600, 72)
            server_costs.append((time.perf_counter() - started) * 1000.0)
            norm_sizes.append(len(norm))
            started = time.perf_counter()
            transcode._encode_thumbnail(norm, 320)
            thumb_costs.append((time.perf_counter() - started) * 1000.0)
        report.samples.append(
            Sample(
                name="server.normalize_webp_q72 (0.2.10)",
                runs=runs,
                mean_ms=statistics.fmean(server_costs),
                p50_ms=statistics.median(server_costs),
                min_ms=min(server_costs),
                max_ms=max(server_costs),
                mean_bytes=int(statistics.fmean(norm_sizes)),
                note="ingest.py:41-45 quality=jpeg_quality(默认72)",
            )
        )
        report.samples.append(
            Sample(
                name="server.thumbnail_320_q68 (0.2.10)",
                runs=runs,
                mean_ms=statistics.fmean(thumb_costs),
                p50_ms=statistics.median(thumb_costs),
                min_ms=min(thumb_costs),
                max_ms=max(thumb_costs),
            )
        )
    except Exception as exc:  # noqa: BLE001
        report.unavailable.append(f"legacy server transcode unavailable: {exc}")


def bench_envelope(runs: int, report: Report) -> None:
    """v3 信封编解码吞吐；W1 起改为 import observatory.link.envelope。"""

    try:
        from observatory.link.envelope import decode, encode  # type: ignore
    except Exception as exc:  # noqa: BLE001
        report.unavailable.append(f"v3 envelope not available yet (W1): {exc}")
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
    parser.add_argument("command", choices=["frames", "envelope", "all"])
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = Report(machine=_machine())
    if args.command in {"frames", "all"}:
        bench_frames(args.runs, report)
    if args.command in {"envelope", "all"}:
        bench_envelope(max(200, args.runs * 20), report)

    if args.json:
        print(json.dumps({"machine": report.machine, "samples": [asdict(s) for s in report.samples], "unavailable": report.unavailable}, ensure_ascii=False, indent=2))
    else:
        print(report.to_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))