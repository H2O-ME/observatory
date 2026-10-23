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

## 4. N4 判据（同轮配对，2026-10-24 裁决）

**绝对毫秒不可跨时段/跨机比较**：同一台机器不同时段能差 2 倍（本轮旧实现 normalize 实测
570–725 ms，而 §2 记录的 312 ms 是另一时段）。因此 N4 只看**当轮配对**：

| 指标 | 判据 | 说明 |
|---|---|---|
| normalize p50 | `new/old` 中位比 ≤ 1.10 | 同一合成输入、交替顺序、同进程 |
| normalize 体积 | `new/old` 平均字节比 ≤ 1.05 | 同质量参数 q72 |
| thumbnail320 p50 | `new/old` 中位比 ≤ 1.10 | 同一份归一化 WebP，q68 |
| thumbnail 体积 | `new/old` 比 ≤ 1.05 | |
| idle RSS | ≤ 1.2 × 29 MB | bench 子进程 `rss.idle` 探针 |
| 连续 20 帧 media RSS 增量 | ≤ 1.2 × 21.6 MB | bench `rss.delta_after_media` |

0.2.10 转码实现已从产品代码删除，其两个纯函数冻结为
`tests/perf/legacy_transcode_0_2_10.py`，测试在同轮内 old/new 交替测量并取比值；
`tools/bench.py media` 输出 `metric / new / baseline / ratio / verdict` 表，供人工复核。

### 4bis. 合成输入的同轮配对实测（2026-10-24，本机）

使用 `tools/bench.py media` 的确定性 1600×900 合成 JPEG（不依赖桌面抓屏）：

| 指标 | 0.2.10 冻结函数 | 0.3.0 media | 配对比 | 结论 |
|---|---:|---:|---:|---|
| normalize p50 | 417.71 ms | 208–492 ms（随负载） | 1.02×（配对中位） | PASS |
| normalize 体积 | 11 532 B | 11 532 B | 1.00× | PASS |
| thumbnail320 p50 | 78.45 ms | 40–84 ms | 1.03× | PASS |
| media 连续 20 帧 RSS 增量 | 21.6 MB（0.2.10 基线） | <27.2 MB | ≤1.2× | PASS |

> 表中 old 列的绝对值仅记录当轮测量；判定以 `tests/perf/test_baseline.py` 现场配对
> 结果为准。§1/§2 的抓屏数字保留为 0.2.10 历史证据，不再作为门限。

## 5. 复现命令

```powershell
# N4 主入口：0.3.0 media 纯转码 + 对照表（任何平台可跑）
.venv\Scripts\python.exe tools\bench.py media --runs 10

# 机器可读结果
.venv\Scripts\python.exe tools\bench.py media --runs 10 --json

# 同轮配对判据（含冻结 0.2.10 函数，跑 Pytest）
.venv\Scripts\python.exe -m pytest tests/perf -q
```

0.2.10 采集实现被删除后，`tools/bench.py frames` 只报告历史基线 + `rss.idle` 探针，
退出码仍为 0；整帧/RSS 回归不再由它判定。