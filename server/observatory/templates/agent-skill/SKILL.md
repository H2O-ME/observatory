---
name: observatory-console
description: 通过 Observatory 控制台 API 管理远端 Windows 设备：设备列表与状态、远程命令执行（SYSTEM）、文件管理（分片读写）、截图与定时截图、时间线、审计、设置。所有调用只需一个 Bearer API 密钥。
metadata:
  version: "@@VERSION@@"
  console: "@@CONSOLE_HOST@@"
---

# Observatory 控制台 API（本地 Agent 接入）

管理远端 Windows 设备机房的完整能力面。认证是**一把长效 API 密钥**（控制台
"Agent 接入"页生成，留空禁用），没有登录限速，改密不吊销。

## 环境变量

```
OBS_API_BASE=https://@@CONSOLE_HOST@@    # 控制台地址（备用：https://observatory.123423.xyz）
OBS_API_KEY=obs-xxxxxxxxxxxxxxxxxxxxxxxx # Bearer 密钥（必填）
OBS_ALLOW_PRIVATE=1   # 仅当本机代理 fake-ip DNS（Clash TUN，解析成 198.18.x.x）或内网控制台
OBS_ALLOW_HTTP=1      # 仅本地 http 调试；默认强制 https（Bearer 不走明文）
```

## 响应信封与错误码（先读这节，避免取错层级）

**所有** console 端点返回信封 `{"code":"OK","data":{…},"server_time_ms":…}`。
注意双层嵌套：`/files/op`、`/commands` 的业务数据在 **`data.data`** /
回执字段在 `data.result`——`envelope.data.data.entries` 才是文件列表。

| code | HTTP | 含义 |
|---|---|---|
| `OK` | 2xx | 成功 |
| `BAD_REQUEST` / `UNSUPPORTED` | 422 | 参数不合法 / 设备不支持或状态不允许 |
| `AUTH` | 401 | **密钥无效或未设置**（检查 OBS_API_KEY，不要重试） |
| `GONE` | 410 | **设备不存在或已删除**（不要重试） |
| `RATE` | 429 | 限流（等 1s 重试） |
| `TIMEOUT` | 504 | 命令在预算内没跑完 |
| `INTERNAL` | 5xx | 服务端错误（重试一次） |

## 设备与状态

| 方法 路径 | 用途 |
|---|---|
| `GET /api/v1/devices` | 设备列表与数量（`presence.state`/`transport`/版本/心跳） |
| `GET /api/v1/devices/{uid}` | 详情：`state`、`capabilities`、`capture`、`sysinfo` |
| `PATCH /api/v1/devices/{uid}` | 改备注 `{"note":…}` / per-device 设置 `{"settings":{…}}` |
| `GET /api/v1/system` | 服务端状态（磁盘、载波、安装端点） |
| `GET /api/v1/audit?limit=&offset=` | 审计（分页 offset） |

**下发前先看 capabilities（A6）**：`GET /devices/{uid}` 里
`capabilities.{control,files,screenshot,update,uninstall}`。离线设备
`control:false`，此时下发命令只会排队到超时——先处理设备在线问题。

## 远程命令

| 方法 路径 | 用途 |
|---|---|
| `POST /api/v1/devices/{uid}/commands` | 下发：`{"type":"…","payload":{…},"confirm":bool}` |
| `GET /api/v1/devices/{uid}/commands/{id}` | 轮询回执（**终态集合：ok/error/timeout/cancelled/failed/unconfirmed**） |
| `GET /api/v1/devices/{uid}/commands?limit=50` | 命令流水 |

命令类型与 payload：
- `exec` — **SYSTEM 身份执行 shell**：`{"command":"<命令>","timeout_s":60}`。
  `timeout_s` 合法区间 5..600（**服务端钳制**，传 9999 会被改成 600 执行）。
  **必须 `confirm:true`**。回执 `result`：`{exit_code,stdout,stderr,
  stdout_truncated,stderr_truncated,duration_ms,timed_out}`；超时
  `ok=false, error_code="TIMEOUT"`，`result` 里带已捕获的部分输出。
  stdout 截断 256KB。锁屏/无人登录也可执行；设备低于 0.3.18 回 `UNSUPPORTED`。
- `screenshot` / `collect_now` — 立即截屏（payload 留空）。
- `sysinfo` — 系统信息快照。
- `log_tail` — `{"role":"agent|worker-1|update","lines":≤5000}`。
- `files` — 控制台通道只转发 list/stat 等非读写操作；**读写一律走 /files/raw**（下节）。
- `reboot` / `poweroff` / `restart_agent` / `uninstall` — 破坏性，必须 `confirm:true`。
- `update` — `{"version":"x.y.z","sha256":"<64hex>"}`，摘要必须匹配 Release 资产。

## 文件管理（读写协议：/files/raw）

非读写操作走 `POST /api/v1/devices/{uid}/files/op`，`op ∈
list|stat|mkdir|delete|rename|copy|move|search|drives|hash`（业务数据在
`data.data`）。**`op:"read"`/`op:"write"` 已废弃，服务端直接 422。**

读写走分片端点（单片 ≤1MiB，文件总量受设备 `file_upload_max_mb`，默认 512MB）：

```
GET /api/v1/devices/{uid}/files/raw?path=<URL编码路径>&offset=0&length=1048576
  → 200 二进制响应体（原始字节）
    响应头：X-File-Size（总大小）、X-Chunk-Offset、X-Chunk-Length、
            X-Eof（"1"=读完了）
  → 循环：offset += 本片长度，直到 X-Eof=1 或 0 字节

PUT /api/v1/devices/{uid}/files/raw?path=<URL编码路径>&offset=0&truncate=false
  → 请求体 = 原始字节（≤1MiB）；首片带 truncate=true 覆盖旧文件
  → 空 body 必须带 truncate=true（清空/建空文件的唯一方式）
```

写操作受设备设置 `file_write_enabled` 门控（关着回 422）。

## 截图与时间线

| 方法 路径 | 用途 |
|---|---|
| `POST /api/v1/devices/{uid}/commands` | `{"type":"screenshot"}` |
| `GET /api/v1/devices/{uid}/frames/latest` | 最新一帧元数据 |
| `GET /api/v1/devices/{uid}/frames/{id}?variant=full\|thumb` | 帧图片二进制（**WebP**，魔数 `RIFF…WEBP`，按 .webp 存） |
| `GET /api/v1/devices/{uid}/frames?from_ms&to_ms&limit` | 帧列表（分页） |
| `GET /api/v1/devices/{uid}/timeline?from_ms&to_ms&bucket_ms` | 活动时间线 |

**截图新鲜度（A8，最容易踩）**：`screenshot` 命令 `status=ok` 只代表设备执行了
截图动作，`frames/latest` 可能仍是**几分钟前**的旧帧（周期采集有自己的节律）。
正确姿势：记下下发时刻的 `server_time_ms`，等命令终态后轮询
`GET /frames/latest`，直到 `frame.ts_ms > 下发时刻` 才算拿到新图（30s 内没有就
是异常）。定时截图（如每天 9:00）用本地调度器在时刻 T 调 `screenshot` + 上述
新鲜度轮询。

## 设置

| 方法 路径 | 用途 |
|---|---|
| `GET /api/v1/settings` | 全局设置（secret 不回显） |
| `PATCH /api/v1/settings` | `{"values":{"键":值}}`（bool/数字自动转换） |
| `GET /api/v1/settings/effective/{uid}` | 设备最终生效值（全局 + 设备覆盖） |

常用键：`capture_interval_s`（采集间隔）、`exec_enabled`（远程命令，默认开）、
`file_write_enabled`、`control_fps/control_quality/control_max_width`（实时控制）。

## 其他

- 备用域名：`https://observatory.123423.xyz`（主域名不可达时切换）。
- 自举安装：`GET /api/v1/public/install.ps1`（受控端安装脚本）、
  `GET /api/v1/public/bootstrap`（服务地址/时间）、`GET /api/v1/public/agent/latest`
  （最新受控端包）——公开端点，无需认证。
- 本 skill 的 `metadata.version` 与服务端版本可能漂移：端点行为以
  `GET /api/v1/console/agent-skill/status` 返回的 `version` 为准重新下载。

## 使用守则

1. **exec 是 SYSTEM 权限**——相当于坐在那台机器的管理员面前。删除、改配置、
   装卸软件前先想一步；命令原文进 commands 表与审计，可追溯。
2. 破坏性命令必须 `confirm:true`（服务端硬校验）。
3. 下发后轮询 `GET commands/{id}`（2-5s 节律）；同语义命令别重复下发（同 id
   重投拿缓存回执，不同 id 会并发跑两份）。
4. 设备离线时命令排队（journal），设备回连后补投；超过预算未执行标 `timeout`。
5. 不要高频打服务端：命令轮询 2-5s、设备列表 10s 级、审计/时间线按需。

## 快速上手（curl）

```bash
BASE=https://@@CONSOLE_HOST@@; KEY=$OBS_API_KEY
# 设备列表
curl -s -H "Authorization: Bearer $KEY" $BASE/api/v1/devices
# 远程执行（SYSTEM）
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"type":"exec","payload":{"command":"whoami","timeout_s":60},"confirm":true}' \
  $BASE/api/v1/devices/OBS-xxxx/commands
# 轮询回执
curl -s -H "Authorization: Bearer $KEY" $BASE/api/v1/devices/OBS-xxxx/commands/<id>
# 读文件（分片，循环到 X-Eof=1）
curl -s -H "Authorization: Bearer $KEY" \
  "$BASE/api/v1/devices/OBS-xxxx/files/raw?path=C%3A%5Ca.log&offset=0&length=1048576" -o chunk.bin
```

## 配套 CLI（scripts/obs.py）

全能力的薄封装，退出码 0/3/4/5 区分成败：

```bash
python obs.py devices                 # 设备表
python obs.py exec OBS-xxxx "cmd" --yes          # SYSTEM 执行（--dry-run 先看请求体）
python obs.py get OBS-xxxx "C:/a.log" a.log      # /files/raw 下载
python obs.py put OBS-xxxx a.log "C:/a.log" --truncate   # /files/raw 上传
python obs.py shot OBS-xxxx --out f.webp         # 等**新鲜帧**再落盘
python obs.py files OBS-xxxx drives              # 列盘符
python obs.py settings --set exec_enabled=true   # 全局设置
python obs.py reboot OBS-xxxx --yes              # 破坏性操作都要 --yes
python obs.py raw GET /api/v1/system             # 透传任意端点（二进制要 --out）
```
