---
name: observatory-console
description: 通过 Observatory 控制台 API 管理远端 Windows 设备：设备列表与状态、远程命令执行（SYSTEM）、文件管理、截图与定时截图、时间线、审计、设置。所有调用只需一个 Bearer API 密钥。
---

# Observatory 控制台 API（本地 Agent 接入）

管理远端 Windows 设备机房的完整能力面。认证是**一把长效 API 密钥**（控制台
"设置 → 安全"里设置，留空禁用），没有登录限速，改密不吊销。

## 环境变量

```
OBS_API_BASE=https://@@CONSOLE_HOST@@   # 控制台地址
OBS_API_KEY=obs-xxxxxxxxxxxxxxxxxxxxxxxx # Bearer 密钥（必填）
```

所有请求带 `Authorization: Bearer $OBS_API_KEY`。响应统一为信封
`{"code":"OK","data":{...},"server_time_ms":...}`；错误为
`{"code":"BAD_REQUEST|UNSUPPORTED|...","message":"..."}`，HTTP 4xx/5xx。

## 端点速查

### 设备与状态
| 方法 路径 | 用途 |
|---|---|
| `GET /api/v1/devices` | 设备列表（在线状态、版本、CPU/内存、最后心跳） |
| `GET /api/v1/devices/{uid}` | 单设备详情（capabilities/state/sysinfo） |
| `PATCH /api/v1/devices/{uid}` | 改备注 / per-device 设置覆盖 `{"settings":{"键":值}}` |
| `GET /api/v1/system` | 服务端状态（磁盘、载波、数量） |
| `GET /api/v1/audit?limit=50` | 审计（谁在什么时候对哪台设备做了什么） |

### 远程命令（含 SYSTEM 执行）
| 方法 路径 | 用途 |
|---|---|
| `POST /api/v1/devices/{uid}/commands` | 下发命令，body `{"type":"…","payload":{…},"confirm":bool}` |
| `GET /api/v1/devices/{uid}/commands/{id}` | 轮询回执（终态：ok/error/timeout/cancelled/failed/unconfirmed） |
| `GET /api/v1/devices/{uid}/commands?limit=50` | 命令流水 |

命令类型与 payload：
- `exec` — **SYSTEM 身份执行 shell 命令**：`{"command":"<命令>","timeout_s":60}`（5..600）。
  **必须 `confirm:true`**。回执 `result`：`{exit_code,stdout,stderr,stdout_truncated,
  stderr_truncated,duration_ms,timed_out}`；超时 `ok=false, error_code="TIMEOUT"`，
  `result` 里带已捕获的部分输出。stdout 截断 256KB。锁屏/无人登录也可执行。
- `screenshot` / `collect_now` — 立即截屏（payload 留空）。
- `sysinfo` — 系统信息快照。
- `log_tail` — `{"role":"agent|worker-1|update","lines":200}`。
- `files` — 文件操作走专用端点（见下），不要用这个类型。
- `reboot` / `poweroff` / `restart_agent` / `uninstall` — 破坏性，必须 `confirm:true`。
- `update` / `reload_config` — 升级与重载配置。

### 文件管理
| 方法 路径 | 用途 |
|---|---|
| `POST /api/v1/devices/{uid}/files/op` | `{"op":"list|stat|mkdir|delete|rename|copy|move|search|drives|read|write|hash", …}` |

常用形状：`{"op":"list","path":"C:\\"}`；`{"op":"read","path":"…","offset":0,"length":700000}`；
`{"op":"write","path":"…","data_base64":"…","append":false}`（单片段 ≤1MB）；
`{"op":"delete","path":"…"}`。回执 `data` 里是 entries/内容（base64）。

### 截图与时间线
| 方法 路径 | 用途 |
|---|---|
| `GET /api/v1/devices/{uid}/frames/latest` | 最新一帧（`full`/`thumb` 变体另行下载） |
| `GET /api/v1/devices/{uid}/frames/{id}?variant=full` | 下载帧图片（JPEG/WebP 二进制） |
| `GET /api/v1/devices/{uid}/timeline?from_ms&to_ms&bucket_ms` | 活动时间线 |
| `GET /api/v1/devices/{uid}/frames?from_ms&to_ms&limit` | 帧列表（分页） |

**定时截图**（如"每天 9:00、18:00 各一张"）：控制台本身已按
`capture_interval_s` 连续采集；定点截图用本地调度器（cron/计划任务）在时刻
`T` 调 `POST commands {"type":"screenshot"}`，等终态后拉 `frames/latest` 即可。

### 设置
| 方法 路径 | 用途 |
|---|---|
| `GET /api/v1/settings` | 全局设置（secret 不回显） |
| `PATCH /api/v1/settings` | `{"values":{"键":值}}` |
| `GET /api/v1/settings/effective/{uid}` | 该设备最终生效值 |

## 使用守则

1. **exec 是 SYSTEM 权限**——相当于坐在那台机器的管理员面前。删除、改配置、
   装卸软件前先想一步；所有命令原文进 commands 表与审计，可追溯。
2. 破坏性命令（`reboot`/`poweroff`/`uninstall`/`restart_agent`/`exec`）必须
   `confirm:true`，这是服务端硬校验。
3. 命令下发后轮询 `GET commands/{id}`，响应里 `deadline_ms` 是服务端给该命令
   的预算；`status` 到终态前不要重复下发同语义命令（同 id 重投会拿缓存回执）。
4. 设备离线时命令会排队（journal），设备回连后补投；超过预算未执行则标
   `timeout`。
5. 轮询节律：命令每 2-5s 查一次即可；设备列表可 10s 级。不要高频打服务端。

## 快速上手（curl）

```bash
BASE=https://control.tianhw.top; KEY=$OBS_API_KEY
# 设备列表
curl -s -H "Authorization: Bearer $KEY" $BASE/api/v1/devices
# 远程执行（SYSTEM）
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"type":"exec","payload":{"command":"whoami","timeout_s":30},"confirm":true}' \
  $BASE/api/v1/devices/OBS-xxxx/commands
# 拿回执（轮询）
curl -s -H "Authorization: Bearer $KEY" $BASE/api/v1/devices/OBS-xxxx/commands/<id>
# 截图
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"type":"screenshot"}' $BASE/api/v1/devices/OBS-xxxx/commands
```

## 配套 CLI

本 skill 附带 `scripts/obs.py`——上面所有能力的薄封装（设备/状态/exec/截图/
文件/日志/通用透传），用法 `python obs.py --help`。它同样只依赖两个环境变量。
