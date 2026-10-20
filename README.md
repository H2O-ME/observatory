# Observatory

自托管的**授权设备观测与远程管理**系统：一台控制服务器 + Windows 受控端 + 控制台 WebUI。

> **授权使用**：本系统只用于你拥有或已获得书面授权的设备。详见 [docs/合规与授权.md](docs/合规与授权.md)。

当前版本：**0.2.0**（见仓库根目录 [`VERSION`](VERSION)）

---

## 0.2.0 做了什么

这一版是一次重写，不是补丁。上一版的核心问题不是某个功能坏了，而是**同一个事实在不同地方有不同答案**：设备列表说在线、文件管理说离线；显示还在上报、功能全不可用；时间线要等一分多钟。根因是每个页面各自推断状态、每条命令各自实现等待。

0.2.0 用三条硬规则取代那种拼凑：

1. **单一事实来源**。设备是否在线只由服务端 `services/presence.py` 计算一次，列表、详情、文件管理、实时控制都读同一个 `presence` 对象（含 `reason` 与 `capabilities`）。前端不自己算在线状态，也不用本机时钟判断新鲜度——每个响应都带 `server_time_ms`。
2. **命令只有一条总线**。任何下发都经过 `services/bus.py`：WebSocket 推送与 HTTP 轮询都汇合到同一个 `settle()`，请求方最多等待有限时间，不再有 20 秒阻塞的 `fs.list`。
3. **通道分级且诚实降级**。受控端优先 WebSocket；不通就退到 HTTPS 长轮询，再退到短轮询，并定期向上探测。降级时界面明确写"仅轮询，实时控制不可用"，而不是假装一切正常。

## 主要能力

| 能力 | 说明 |
| --- | --- |
| 画面采集 | 按会话采集、变化检测、WebP 压缩、缩略图与时间线按桶聚合 |
| 实时控制 | WebSocket 中转鼠标/键盘注入，需显式授权、全程审计、TTL 自动结束 |
| 文件管理 | 资源管理器级：驱动器、列目录、新建、改名、复制、移动、删除、搜索、分片读写（≥1 MiB 随便传） |
| 自动更新 | 安装一次，之后服务端上传新包即自动替换，无 UAC、无重装 |
| 远程卸载 | 受控端服务以 SYSTEM 自我清理，**不弹 UAC** |
| 双域名 | 两个域名互为备份提供安装与受控端接口；控制台只在 `control.tianhw.top` |
| 版本机制 | 仓库根部 `VERSION` 是唯一来源，控制台/接口/安装脚本/受控端显示同一个号 |

## 架构

```
┌────────────┐   HTTPS/WSS    ┌──────────────────────────┐
│ 控制台 WebUI│ ─────────────▶ │ 控制服务器 (FastAPI)      │
│ React/Vite │ ◀───────────── │ presence / bus / storage │
└────────────┘   server_time  └───────────┬──────────────┘
                                          │ HTTPS/WSS
                              ┌───────────▼──────────────┐
                              │ 受控端服务 (SYSTEM)        │
                              │ 传输阶梯 + 更新 + 卸载     │
                              └───────────┬──────────────┘
                                  命名管道 │
                              ┌───────────▼──────────────┐
                              │ 会话工作进程 (用户会话)     │
                              │ mss 采集 / SendInput / 文件│
                              └──────────────────────────┘
```

受控端必须分成两个进程：采集桌面与注入输入只能发生在**用户会话**里，而自动更新与卸载必须能在**没有用户登录**时以 SYSTEM 完成。两者用命名管道通讯（`PeekNamedPipe` 轮询，避免同步句柄死锁）。

## 目录

```
VERSION                     唯一版本来源
server/observatory/         控制服务器（Python 3.12）
  api/                      HTTP 与 WebSocket 端点
  services/                 presence / bus / storage / retention / packages ...
  templates/install.ps1     安装脚本模板（服务端渲染，含版本号与注册码）
  templates/install.cmd     双击即用的安装程序模板（内置注册码与多线路地址）
agent/observatory_agent/    受控端（Windows，PyInstaller onedir）
  transport.py              WSS → 长轮询 → 短轮询
  worker.py                 用户会话内：采集、输入、文件
  updater.py / uninstall.py 自动更新与无 UAC 卸载
webui/                      控制台（React 18 + Vite + TS）
deploy/                     compose / nginx / 部署与更新脚本
tools/                      版本工具与自检
usb-installer/              现场安装向导（不含任何硬编码密钥）
```

## 快速开始（本地开发）

```bash
# 1. 服务端
cd server
python -m venv ../.venv && ../.venv/Scripts/pip install -r requirements.txt
cp .env.dev .env                      # 开发用弱口令
cd .. && .venv/Scripts/python -m uvicorn observatory.main:app --app-dir server --port 8443

# 2. 控制台
cd webui && npm install && npm run dev   # 代理到 https://127.0.0.1:8443

# 3. 自检
.venv/Scripts/python tools/test_server.py
```

## 生产部署

```bash
sudo REPO_URL=git@github.com:<you>/observatory.git CERTBOT_EMAIL=you@example.com \
     bash deploy/setup.sh
```

日常更新只需要：

```bash
sudo bash deploy/update.sh
```

详见 [docs/部署.md](docs/部署.md)。

## 版本纪律

* 仓库根目录 `VERSION` 是**唯一**需要人改的地方。
* 下一个大版本之前，所有改动都是补丁号递增：`0.2.0 → 0.2.1 → 0.2.2 …`
* 一条命令同步所有副本并检查漂移：

```bash
python tools/set_version.py --bump      # 0.2.0 -> 0.2.1
python tools/set_version.py --check     # CI 里跑，漂移即失败
```

* 发版：`git tag v0.2.1 && git push --tags`。CI 会校验 tag 与 `VERSION` 一致，并在 Windows runner 上产出受控端安装包。

## 测试

```bash
python tools/test_server.py        # 服务端端到端：版本、鉴权、双域名、命令总线、画面、更新、更新包、控制转发、SPA
python tools/test_agent.py         # 受控端：版本/配置/帧协议/传输阶梯/真实采集与文件操作/卸载清理范围
python tools/test_e2e_live.py      # 真起服务端 + 真起受控端（仅 Windows）：注册→WS→心跳→采集→命令→文件读写→审计
python tools/check_deployment.py --password <密码> --code <注册码>   # 已部署实例的公网体检
cd webui && npm run build          # 类型检查 + 生产构建
python -m compileall -q server/observatory agent/observatory_agent
```

`test_e2e_live.py` 是唯一能证明"界面没有说谎"的测试：它真的启动两个进程，
真的截屏、真的写文件、真的走 WebSocket 下发命令。它不触发远程卸载（那会真的把
受控端自己删掉），卸载契约由 `test_server.py` 覆盖。

## 与 0.1 的不兼容变化

* 服务名、安装目录、数据目录、命名管道、环境变量前缀、容器名全部改名（`OBS_` 前缀，`%ProgramData%\Observatory`）。旧受控端不会自动迁移，需要重新安装一次——这正是 0.2.0 之后自动更新机制存在的意义。
* 设备令牌请求头为 `X-Device-Token`；控制台会话同时支持 `Authorization: Bearer` 与 HttpOnly Cookie（后者用于让 `<img>` 走浏览器缓存）。
* 时间线接口返回聚合桶而不是逐帧列表；画面 URL 不再需要 cache-buster。
* 远程卸载接口**要求**请求体 `{"confirm": true}`。

## 文档

* [部署](docs/部署.md)
* [使用](docs/使用.md)
* [接口](docs/接口.md)
* [受控端打包](docs/受控端打包.md)
* [合规与授权](docs/合规与授权.md)