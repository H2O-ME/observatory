# Observatory

自托管的**授权设备观测与远程管理**系统：一台控制服务器 + Windows 受控端 + 控制台 WebUI。

> **授权使用**：本系统只用于你拥有或已获得书面授权的设备。详见 [docs/合规与授权.md](docs/合规与授权.md)。

当前版本：**0.3.7**（见仓库根目录 [`VERSION`](VERSION)）

---

## 0.3.0 做了什么

0.2.x 的问题不是某个功能坏了，而是**同一个事实有多条实现路径**：WS 与 HTTP 各一套心跳/回执、
两套采集循环、两处在线判定、两份时钟。0.3.0 是一次结构性重写，用 8 条硬约束取代拼凑：

| # | 约束（违反即返工） |
| --- | --- |
| C1 | 设备入网/心跳/命令/回执/事件**只有一条逻辑链路**；WebSocket 与 HTTP 长轮询只是同一链路的两个**载体** |
| C2 | 帧上传**只有一条**：`POST /api/v1/agent/frames`（原始字节），与载体无关 |
| C3 | 命令**只有一个创建点、一个回执点**；等待方 `async with` 作用域退出即清理，无全局 waiter 泄漏 |
| C4 | presence/capabilities **只有一个函数**产出；UI/业务不得直接读通道表 |
| C5 | 时间**只有一个来源**：服务端时钟；响应带 `server_time_ms`，需要预算的带 `deadline_ms`；设备时间命名 `device_time_ms` |
| C6 | 破坏性动作（删除/归档/更新/卸载）**成功或可观测失败**，禁止 fire-and-forget |
| C7 | 旧协议兼容代码**只在 `server/observatory/legacy/`**，新代码不得 import（0.3.1 起该层已整体删除） |
| C8 | 配置项**要么生效要么不存在**；`Settings`/`settings_store` 每个键都必须有消费者 |

0.2.x 受控端的零重装升级路径随 0.3.1 的 legacy 层删除一并退役：设备收到更新包后
自行替换并以 v3 协议重连（sha256 + 预检 + 失败回滚，A3/A7）。

## 主要能力

| 能力 | 说明 |
| --- | --- |
| 画面采集 | 会话内采集、变化检测、WebP 压缩、缩略图、时间线按桶聚合 |
| 实时控制 | 单一链路中继鼠标/键盘；显式授权、TTL、撤销即时生效、全程审计 |
| 文件管理 | 驱动器/列目录/新建/改名/复制/移动/删除/搜索/分片读写（≥1 MiB 任意大小） |
| 自动更新 | 安装一次后服务端上传新包即自动替换；sha256 + 预检 + 失败回滚，无 UAC |
| 远程卸载 | SYSTEM 自我清理，不弹 UAC；失败可观测并保持在线 |
| 双域名 | 两域名互为备份提供安装与设备接口；控制台只在 `control.tianhw.top` |
| 安装凭据 | **一次性安装令牌**：单次使用、TTL（控制台设置，默认 24 小时）、绑定首台设备、可撤销 |
| 版本机制 | 根目录 `VERSION` 是唯一来源，服务端/受控端/安装脚本/控制台同一版本号 |

## 架构

```
┌────────────┐  HTTPS/WSS   ┌──────────────────────────────────────┐
│ 控制台 WebUI│ ───────────▶ │ 控制服务器 (FastAPI)                  │
│ React18+Vite│ ◀─────────── │ link(信封) / presence / media / console│
└────────────┘ server_time  └───────────────┬──────────────────────┘
                                             │ 单链路：WS 载体 / HTTP 长轮询载体
                                ┌────────────▼─────────────────────┐
                                │ 受控端服务进程 (SYSTEM)             │
                                │ link / outbox / update / uninstall │
                                └────────────┬─────────────────────┘
                                    同一信封协议（命名管道）
                                ┌────────────▼─────────────────────┐
                                │ 会话 worker (用户会话) × N          │
                                │ capture / input / fs               │
                                └──────────────────────────────────┘
```

进程模型不变：服务进程（SYSTEM）持有链路与命令分发，worker（用户会话）只做采集/输入/文件；
两者用同一份信封协议走命名管道。区别是**协议与实现都只剩一份**。

### 服务端目录（0.3.0 目标结构）

```
server/observatory/
  main.py            # 装配（薄）：lifespan、路由挂载、中间件
  core/              # config / db(版本化迁移) / clock / crypto / errors / log
  link/              # envelope / session / outbox / registry / carrier_ws / carrier_http / journal
  presence.py        # ★ 唯一在线判定与 capabilities
  media/             # ingest / transcode / storage / retention / archive
  console/           # auth devices files control settings audit updates system + dto
  adapters/          # hostsplit（双域名，fail-closed）
  templates/         # install.ps1 / install.cmd（服务端渲染）
```

### 协议 v3（摘要）

```json
{ "v": 3, "id": "<uuid4>", "t": "<type>", "ts_ms": 1730000000000, "body": { } }
```

- 端点：`POST /api/v1/agent/link`（上行批次 → 下行批次，可 hold ≤20s）、`WS /api/v1/agent/link`、
  `POST /api/v1/agent/frames`（`X-Frame-Id` 幂等）。
- 超时预算一次写死：服务端 deadline = min(类型上限, 25s)，客户端 = deadline + 5s，响应带 `deadline_ms`。
- 错误统一 `{code, message, retryable}`（AUTH/GONE/BAD_REQUEST/UNSUPPORTED/TIMEOUT/RATE/INTERNAL）。
- 旧端点（`/api/v1/agent/{enroll,heartbeat,commands,...}`、`WS /api/v1/agent/ws`）继续可用，由兼容层翻译。

## 目录

```
VERSION                     唯一版本来源
server/observatory/         控制服务器（Python 3.12）
server/observatory/templates/ 安装脚本模板（install.ps1 / install.cmd，服务端渲染）
agent/observatory_agent/    受控端（Windows，PyInstaller onedir）
webui/src/                  控制台（React 18 + Vite + TS）
deploy/                     compose / nginx 模板 / 部署、更新、备份、证书脚本
docs/                       规格、部署、接口、使用、打包、合规
usb-installer/              现场安装向导（不含任何硬编码凭据）
tests/                      unit / contract / acceptance / integration / perf（0.3.0 起）
tools/                      版本工具与对外自检入口
```

## 快速开始（本地开发）

```bash
# 1. 依赖
python -m venv .venv
.venv/Scripts/pip install -r server/requirements.txt -r requirements-dev.txt   # Windows
# .venv/bin/pip install -r server/requirements.txt -r requirements-dev.txt     # Linux/macOS

# 2. 服务端（开发用弱口令；0.3.0 生产启动会拒绝/告警开发凭据）
cd server
cp .env.dev .env
cd ..
.venv/Scripts/python -m uvicorn observatory.main:app --app-dir server --port 8443

# 3. 控制台
cd webui && npm install && npm run dev    # 代理到 https://127.0.0.1:8443

# 4. 自检
.venv/Scripts/python -m pytest tests/ -q
.venv/Scripts/python tools/test_server.py
```

> `server/.env.dev` **只用于开发机**，不要复制到生产（生产用 `deploy/setup.sh` 生成随机密钥）。

## 生产部署

```bash
sudo REPO_URL=git@github.com:<you>/observatory.git \
     CERTBOT_EMAIL=you@example.com \
     bash deploy/setup.sh
```

`setup.sh`（0.3.0）：

1. 校验域名/root；安装 docker + compose（**Debian/Ubuntu 使用各自官方源**）；
2. 安装 nginx + certbot；准备 `server/.env`（随机密钥 + 回环绑定 + 安装令牌 TTL）；
3. 用 ACME bootstrap vhost 占住 80 → `certbot --webroot` 申请**单张双域名证书**；
4. **实体拷贝**证书到 `certs/`（非符号链接），属主为容器用户（默认 UID 10001）；
5. 渲染 nginx 站点模板（域名/证书路径构建期注入）→ `nginx -t` → reload；
6. 构建 `observatory/api:<VERSION>` 并启动；健康检查失败**退出非 0**。

日常更新（0.3.6 起为自动链路）：

```bash
# 发布：一条命令（bump → tag → push → CI → 生产部署）
tools/release.sh
```

0.3.7 起部署是 **CI 直推**：`tools/release.sh` 打 tag → Actions 全门禁（含 Windows
受控端构建）→ `deploy` job SSH 点火服务器 `deploy/deploy.sh`（快备份、幂等 no-op、
多点健康验证、失败自动回滚、熔断）。`main` 普通推送只跑测试不部署。
详见 [docs/发布与更新链路.md](docs/发布与更新链路.md)（手工兜底与排障见 [docs/部署.md](docs/部署.md)）。

## 容器与安全默认值

- 8443 默认只绑 `127.0.0.1`，由本机 nginx 反代（CDN 直连才显式设 `OBS_BIND_ADDR=0.0.0.0`）。
- `OBS_TRUST_FORWARDED_FOR` 默认 false（代码侧）；只有可信反代拓扑才在 `.env` 显式打开。
- 容器非 root（`OBS_UID=10001`）、只读根文件系统、丢弃全部 capabilities、限制内存/CPU/PID。
- 所有密钥/口令仅存 `server/.env`（chmod 600）或数据库哈希；安装包内不再有长期共享注册码。

## 版本纪律

* 仓库根目录 `VERSION` 是**唯一**需要人改的地方。
* 一条命令同步派生副本并检查漂移：

```bash
python tools/set_version.py --bump      # 0.3.0 -> 0.3.1
python tools/set_version.py --check     # CI 里跑，漂移即失败（0.3.0 起覆盖 README/docs）
```

* 发版：`git tag v0.3.0 && git push --tags`。CI 校验 tag 与 `VERSION` 一致，
  在 Windows runner 上构建受控端 zip，并推送到 `agent-dist` 分支 / Release。

## 测试

```bash
python -m pytest tests/ -q            # 0.3.0 主测试套件（unit/contract/acceptance/integration）
python tools/test_server.py           # 对外自检入口（兼容保留）
python tools/test_agent.py            # 受控端逻辑自检（Windows 组自动跳过）
python tools/test_e2e_live.py         # 真起服务端+受控端（仅 Windows）
python tools/check_deployment.py --password <密码> --token <一次性令牌>   # 已部署实例公网体检
# 省略 --token 时会登录后现场签发
cd webui && npm run build             # 类型检查 + 生产构建
```

受控端解析/帧协议/传输阶梯/更新回滚等纯逻辑在任意平台可跑；真实采集、输入、文件与卸载清理
只在 Windows 上执行。CI 门禁（A20）：服务端自检、受控端逻辑测试、协议契约、WebUI 类型检查与构建。

## 兼容性

* 数据平滑迁移：启动时版本化迁移，迁移前自动 `SQLite backup` 到 `data/backups/pre-migrate-<version>.db`（N3）。
* 安装凭据只有一次性安装令牌（单次使用、TTL、绑定首台设备）；共享注册码已于 0.3.1 移除。

## 文档

* [0.3.1 修复规格（当前唯一事实来源）](docs/0.3.1-修复规格.md)
* [0.3.0 重构规格（历史规格，含 0.3.1 变更注记）](docs/0.3.0-重构规格.md)
* [部署](docs/部署.md) · [使用](docs/使用.md) · [接口](docs/接口.md)
* [受控端打包](docs/受控端打包.md) · [合规与授权](docs/合规与授权.md)
* [项目通读报告（0.2.10 审计）](docs/项目通读报告.md)