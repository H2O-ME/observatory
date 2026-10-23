# N4 性能基线（0.2.10 实测 → 0.3.0 对照）

> 采集时间：2026-10-23（0.2.10 代码删除前）
> 采集方式：`python tools/bench.py frames --runs N`（本文件表格 = `--runs 6/8` 的实测输出）
> 采集脚本：`tools/bench.py`（W2 起接新实现，命令与输出格式不变）
> 回归测试：`tests/perf/test_baseline.py`（W3 启用）

## 0. 采集环境（必须随结果一起记录）

| 项 | 值 |
|---|---|
| 平台 | Windows（本机开发环境，详情见 `tools/bench.py --json` 的 `machine`） |
| Python | 3.12（仓库 `.venv`） |
| 采集实现 | 0.2.10 `agent/observatory_agent/capture.py::grab(max_width=1600, quality=72)` |
| 服务端实现 | 0.2.10 `server/observatory/services/transcode.py`（`_normalise_frame` / `_encode_thumbnail`） |
| 屏幕 | 1600×900（`grab` 后宽度被限制到 1600） |

> ⚠️ 本机是远程/虚拟桌面会话，抓屏成本远高于物理机。**绝对毫秒数不能跨机比较**；
> N4 的判据是**同机前后相对比较**：耗时 ≤ +10%、帧体积 ≤ +5%、空闲 RSS ≤ 1.2×
> （spec §3.2 已裁决）。下表数字只作同机回归的参照点，**76ms 是历史文档里另一台
> 物理机的数字，不再作为门限**（本机复现不出来）。

## 1. 受控端整帧（0.2.10 实测）

| 指标 | 运行次数 | 均值 ms | P50 ms | 最小 ms | 最大 ms | 平均字节 | 备注 |
|---|---:|---:|---:|---:|---:|---:|---|
| frame.grab+webp_q72 (cold, 首次) | 1 | 699.25 | 699.25 | 699.25 | 699.25 | 13 960 | 含 mss/PIL 懒初始化 |
| frame.grab+webp_q72 (run A, warm) | 8 | 390.03 | 312.27 | 298.11 | 699.25 | 13 740 | 稳态；本机抓屏开销主导 |
| frame.grab+webp_q72 (run B, warm) | 6 | 389.75 | 390.24 | 311.59 | 443.92 | 20 244 | 桌面内容不同，体积差异大 |

> 说明：两次 run 的均值一致（≈390ms），p50 在 312–390ms 间波动，取决于桌面变化量。
> N4 阈值取两次中更保守的值：p50 390.24ms、体积 20 244B。

## 2. 服务端转码（0.2.10 实测）

| 指标 | 运行次数 | 均值 ms | P50 ms | 最小 ms | 最大 ms | 平均字节 | 备注 |
|---|---:|---:|---:|---:|---:|---:|---|
| server.normalize_webp_q72 | 6 | 336.45 | 312.16 | 254.32 | 489.36 | 26 042 | `ingest.py:41-45` 用 `jpeg_quality` 默认 72 |
| server.thumbnail_320_q68 | 6 | 59.70 | 55.43 | 45.66 | 84.89 | — | `transcode.py:19` THUMB_QUALITY=68 |

**规格与代码不一致（需 release-ops / server-media 注意）**：规格 §3.2 写「服务端 WebP q75」，
0.2.10 实际是 `quality=values.get("jpeg_quality", 72)`（`server/observatory/services/ingest.py:41,45`）。
0.3.0 若改成 q75，帧体积会上升，N4 的"体积不退化"阈值需要按新质量重新定基线。

## 3. 内存（0.2.10 实测，Python 进程 RSS）

| 指标 | 值 | 备注 |
|---|---:|---|
| 采集模块导入后（idle） | ≈29 MB | 新进程 import `observatory_agent.capture` 后 |
| 连续 6 帧后 | ≈50 MB | +21.6 MB（mss 帧缓冲 + PIL 中间图，未释放回 OS） |

> 0.3.0 目标：idle RSS 不高于本表；连续采集后 RSS 增长 ≤1.25× 本表增量。
> W3 需用可执行产物再测一次（`tasklist`/psutil），本表是源码级基线。

## 4. N4 判据（W1 配对结论 + 本机记录基线，2026-10-24 裁决）

0.2.10 旧实现已删除，**不可能再同轮配对**；W1 删除前的配对实测是最终证据：

| 指标 | W1 配对结果（old→new） | 结论 |
|---|---:|---|
| normalize p50 | **0.981×** | PASS（≤1.10×） |
| thumbnail320 p50 | **1.009×** | PASS（≤1.10×） |
| 帧/normalize 体积 | **1.000×** | PASS（≤1.05×） |
| RSS idle | **0.995×** | PASS（≤1.2×） |
| 连续 20 帧 media RSS 增量 | **≈1.00×** | PASS（≤1.2×） |
| `store_frame`（落盘+入库） | **0.968×** | PASS |

**绝对毫秒随机器负载漂移**：今天旧实现 normalize 实测 570–725 ms，而 §2 的 312 ms 是
另一时段/负载；390 ms 同理只能当历史记录，不能当门限。因此自动化判据只允许在
**记录基线的同一台测量机**上比较（`tests/perf/test_baseline.py` 记录 OS/CPU/python 指纹，
不符时 `pytest.skip(reason)`）：

| 指标 | 记录基线（本机） | 判据 |
|---|---:|---|
| normalize p50（合成 1600×900） | 417.71 ms | new ≤ +10% |
| normalize 体积 | 11 532 B | new ≤ +5% |
| thumbnail320 p50 | 78.45 ms | new ≤ +10% |
| idle RSS（bench 子进程） | 29.0 MB | new ≤ 1.2× |
| 连续 20 帧 media RSS 增量 | 21.6 MB | new ≤ 1.2× |

> 这些是"本机记录的 new 基线"（0.2.10 W1 配对时测得的绝对值），不是跨机门限。
> 换机器/换时段跑时只记录数字，不做绝对毫秒断言。

`tools/bench.py media` 输出 `metric / new / baseline / ratio / verdict` 表，供人工复核；
`tests/perf/test_baseline.py` 打印 `[N4] 指标 new/baseline/ratio` 行，失败时能直接看到数字。

## 5. 复现命令

```powershell
# N4 主入口：0.3.0 media 纯转码 + 对照表（任何平台可跑）
.venv\Scripts\python.exe tools\bench.py media --runs 10

# 机器可读结果
.venv\Scripts\python.exe tools\bench.py media --runs 10 --json

# N4 判据（默认回归不跑；本机 perf 模式或 CI perf job）
.venv\Scripts\python.exe -m pytest tests/perf tests/acceptance/test_n1_n4.py -m perf -q
```

`pytest.ini` 默认 `addopts = ... -m "not perf"`，性能测试不会混进日常回归；
只有 CI 的 perf job（手动触发）或本机显式 `-m perf` 才执行。0.2.10 采集实现删除后，
`tools/bench.py frames` 只报告历史基线 + `rss.idle` 探针，退出码仍为 0。