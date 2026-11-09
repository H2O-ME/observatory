---
name: observatory-console
description: 通过 Observatory 控制台 API 管理远端 Windows 设备：设备列表与状态、远程命令执行（SYSTEM）与长任务/脚本下发/批量扫描调试守则、文件管理（分片读写）、截图与定时截图、时间线、审计、设置。所有调用只需一个 Bearer API 密钥。
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

PUT /api/v1/devices/{uid}/files/raw?path=<URL编码路径>&offset=0&truncate=true
  → 请求体 = 原始字节（单片 ≤1MiB）；**首片必须带 truncate=true**
  → 续片用 offset=<已写字节数>&truncate=false
  → 空 body 首片也必须带 truncate=true（清空/建空文件的唯一方式）
```

写操作受设备设置 `file_write_enabled` 门控（关着回 422）。

**⚠️ 覆盖已有文件的铁律（实测事故）**：PUT 是**随机写**，
`offset=0&truncate=false` 只覆盖文件头部、不截断尾部——把更短的脚本/配置
重传到同一路径，旧文件的尾部字节会残留拼接在新内容后面（`.ps1` 表现为
"表达式或语句中包含意外的标记"解析错误，且排查极费时）。两种正确写法：

1. 首片带 `truncate=true`（推荐，一次 PUT 完成重建，`obs.py put --truncate`）；
2. 或先 `POST /files/op {"op":"delete","path":…}` 再传。delete 对不存在的
   文件会回 422 `BAD_REQUEST "找不到文件或路径（2）"`，脚本化重传前要**容错
   422**（当成功处理）。注意 `total` 参数不是服务端契约，勿依赖；分片边界按
   实际字节数累加。

## 远程执行与调试实战守则（长期任务 / 脚本下发 / 批量扫描）

这一节是 SYSTEM 远程执行踩过的坑，写脚本前先读。

### 1. exec 会杀整棵进程树，长任务不能同步跑

- `exec` 的 `timeout_s` 上限 600s（服务端钳制）。**超时或调用方停止轮询后，
  受控端执行 `taskkill /T /F` 杀掉整棵进程树**（含所有孙进程）——这是
  0.3.20/0.3.21 事故修复带来的既定行为，不是 bug。
- 因此**任何预计超过几十秒的作业都不要同步 exec**。同步跑 10 分钟级扫描的
  典型症状：日志只有 "start" 一行，之后进程人间蒸发，回执 `POLL_DEADLINE_EXCEEDED`
  或 TIMEOUT，设备侧什么都没留下。
- 正确姿势：把作业写成设备上的 `.ps1`/`.py`，由**启动器 exec** 脱离拉起，
  之后靠状态文件/日志文件轮询进度：

  ```powershell
  # launcher.ps1（同步 exec 跑，1 秒返回）
  Start-Process -FilePath 'powershell.exe' `
    -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass', `
                 '-File','C:\Windows\Temp\obs-job.ps1' -WindowStyle Hidden
  ```

  作业脚本自己写 `Add-Content` 进度日志与结果文件（每完成一个批次落一行），
  查询端只 `Get-Content … | Select-Object -Last 5`。**用户反感阻塞式轮询**——
  后台脚本 + 状态文件是标准做法。

### 2. 脱离拉起也可能活不下来，必须有存活自检

`Start-Process` 脱离并非绝对保险：实测同一手法在多数场景能活，但 exec 收尾
清扫仍可能 ~30s 后杀掉分离子进程。备选方案及其坑：

| 拉起方式 | 实测结果 |
|---|---|
| `Start-Process … -WindowStyle Hidden` | 多数可用；偶发被杀 |
| WMI `Invoke-CimMethod Win32_Process Create` | 有时能活（父进程是 WmiPrvSE），不同机器表现不一致 |
| `schtasks /Create /Run` | SYSTEM 会话下报"帐户名与安全标识间无任何映射"，不可用；`/SD` 日期必须 `yyyy/mm/dd` |
| 同步 exec 直跑（≤600s 预算内） | **最可靠兜底**：能跑完就一定能拿到结果，代价是占着命令通道 |

**必须做存活自检**，且检查脚本里正则匹配进程时要排除自身 PID：

```powershell
$me = $PID
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -match 'obs-job' -and $_.ProcessId -ne $me }
```

否则检查命令自己的 CommandLine 里含 `obs-job`，永远误报 ALIVE。判断真死活的
依据是**进度日志是否在推进**，不是进程列表的一瞬间快照。

### 3. 下发脚本的编码与语法

- **PowerShell 5.1 把无 BOM 的 UTF-8 脚本按 GBK 读**：中文字符串（含注释）
  的尾字节可能撞坏引号，表现为"字符串缺少终止符"/"表达式中包含意外的标记"。
  传给设备的脚本**一律纯 ASCII**，需要中文输出时用
  `[Text.Encoding]::GetEncoding('GBK')` 在运行时解码/编码。
- **不要在 Bash heredoc / 内联 `powershell -Command` 里拼长脚本**：`$` 会被
  宿主 shell 吞掉、引号层层转义必错。先本地 Write 成 `.ps1`，上传，再用
  `powershell -NoProfile -ExecutionPolicy Bypass -File <路径>` 执行。
- 上传后用 `Parser::ParseFile` 做语法预检，别直接跑：

  ```powershell
  $e = $null
  [System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$null, [ref]$e)
  "PARSEERR=$($e.Count)"
  ```

### 4. 杀进程时别杀到自己

exec 命令文本里如果含目标进程名，受控端（或你自己的 Where-Object 匹配）会
**自匹配**——经典翻车是脚本里写 `Stop-Process` 杀 `cloudflared`，结果把执行
这条命令的 powershell 一起杀了，回执消失。解法：

```powershell
$p = '*cloudfla' + 'red*'   # 字符串拼接，绕开自匹配
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -like $p -and $_.ProcessId -ne $PID } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

固定服务（如正式隧道）要在匹配里显式排除，别用一刀切的通配。

### 5. 批量网络探测（PowerShell runspace 池）

- 作业实例数超 ~500 会把教学机/瘦终端撑崩；**pool ≤250、每个作业处理
  ≥130 个目标**（`perThread = ceil(ips/300)` 量级）——决定并发的是作业数，
  不是池大小。
- 未路由网段 TCP 连接快速失败时，pool 150/200 + `BeginConnect` 超时
  300–400ms 可达到 **~30 个 /24/分钟**（双端口），一个 /16 约 2 分钟。
- 大范围扫描拆成多分片并行（每台设备 4 进程分片），结果/日志按分片 tag
  分文件，避免互相覆盖；每个分片完成写 `ALLDONE` 标记行。
- 临时探测只做 TCP connect + GET 指纹，**不点登录、不爆破**；敏感系统
  弱口令每台最多 1 次，网关永远不碰。

### 6. 代理机 DNS 与 HTTP 客户端

- 本机 Clash fake-ip DNS（域名解析成 198.18.x.x）会把控制台域名也劫持，
  `obs.py`/CLI 需 `OBS_ALLOW_PRIVATE=1`；需要真实解析时用 DoH。
- `HttpWebRequest` 的 `Accept` / `Referer` / `Content-Type` / `Cookie` 是**受保护
  头**，只能走专用属性或 `CookieContainer`，塞进 `Headers` 集合会抛异常。
- 老设备/老 Tomcat 与 Go TLS（cloudflared）握手可能失败，需要 .NET
  `ServerCertificateValidationCallback = { $true }` 剥头代理中转；
  `HttpListener` 绑 **127.0.0.1** 而不是 localhost（Go 先试 ::1 会 502）。

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
6. **长任务用后台脚本 + 状态文件**（见"远程执行与调试实战守则"）：exec 超时
   会连树杀，别同步跑；进度/结果落文件，查询只看尾部几行，不做阻塞轮询。
7. **重传文件必带 `truncate=true` 或先 delete**：PUT 是随机写，短内容覆盖长
   文件会残留旧尾字节。delete 不存在的 422 要容错。
8. **前端/脚本下发前先做语法预检**（`Parser::ParseFile`）；设备侧脚本纯 ASCII，
   避开 PowerShell 5.1 GBK 解析坑。

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
# 上传（首片 truncate=true 覆盖旧文件；续片用 offset&truncate=false）
curl -s -X PUT -H "Authorization: Bearer $KEY" -H "Content-Type: application/octet-stream" \
  --data-binary @local.bin \
  "$BASE/api/v1/devices/OBS-xxxx/files/raw?path=C%3A%5Ca.log&offset=0&truncate=true"
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
