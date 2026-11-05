#!/usr/bin/env python3
"""Observatory 控制台 CLI——本地 agent 管理远端设备的全能力薄封装（纯标准库单文件）。

环境变量：
    OBS_API_BASE        控制台地址（默认 https://@@CONSOLE_HOST@@）
    OBS_API_KEY         Bearer 密钥（必填）
    OBS_ALLOW_PRIVATE   1 = 放行私网/环回/保留地址（本机代理 fake-ip DNS、内网控制台）
    OBS_ALLOW_HTTP      1 = 允许 http:// 明文（仅本地调试；默认强制 https）

退出码：0 成功；2 用法错误；3 认证失败（401）；4 远端命令失败/超时；
5 网络/服务端错误。exec 子命令把远端 exit_code 原样返回（1..255）。

安全语义：
    - DNS 解析后校验 IP 边界，然后**钉住该 IP** 连接（SNI/Host/证书仍按域名），
      校验与连接是同一次解析，无 DNS rebinding 窗口；
    - 不跟随重定向（30x 直接报错，防止密钥被带到别的源站）；
    - 默认拒绝 http:// 与私网地址；OBS_ALLOW_PRIVATE / OBS_ALLOW_HTTP 是显式出口。

用法（完整列表 `python obs.py --help`）：
    python obs.py devices
    python obs.py status OBS-xxxx
    python obs.py exec OBS-xxxx "whoami" --yes [--timeout 60] [--dry-run]
    python obs.py shot OBS-xxxx --out frame.webp      # 等到**新于下发时刻**的帧
    python obs.py files OBS-xxxx list C:\\
    python obs.py get  OBS-xxxx "C:/a.log" local.log   # /files/raw 分片下载
    python obs.py put  OBS-xxxx local.log "C:/a.log"   # /files/raw 分片上传
    python obs.py settings --set exec_enabled=true
    python obs.py reboot OBS-xxxx --yes
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
import time
import urllib.parse

BASE = os.environ.get("OBS_API_BASE", "https://@@CONSOLE_HOST@@").rstrip("/")
KEY = os.environ.get("OBS_API_KEY", "")
TERMINAL = {"ok", "error", "timeout", "cancelled", "failed", "unconfirmed"}
CHUNK = 1024 * 1024  # /files/raw 单分片上限（服务端 MAX_CHUNK_BYTES）
RETRY_STATUS = {429, 502, 503, 504}
HTTP_TIMEOUT_S = 30.0

ALLOW_PRIVATE = os.environ.get("OBS_ALLOW_PRIVATE", "").strip().lower() in {"1", "true", "yes"}
ALLOW_HTTP = os.environ.get("OBS_ALLOW_HTTP", "").strip().lower() in {"1", "true", "yes"}
JSON_MODE = False


class CliError(SystemExit):
    """带退出码的错误：0 成功、2 用法、3 认证、4 远端失败、5 网络/服务端。"""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.message = message


# --------------------------------------------------------------- 传输层 ---
def _validate_target(parsed: urllib.parse.ParseResult) -> tuple[str, int, str]:
    """协议白名单 + IP 边界校验；返回 (ip, port, host)。"""

    host = parsed.hostname or ""
    scheme = parsed.scheme
    if scheme == "https":
        port = parsed.port or 443
    elif scheme == "http":
        if not ALLOW_HTTP:
            raise CliError(5, "拒绝 http:// 明文传输 Bearer 密钥（本地调试请设 OBS_ALLOW_HTTP=1）")
        port = parsed.port or 80
    else:
        raise CliError(5, f"拒绝非 http/https 协议: {scheme!r}")
    if not host or parsed.username or parsed.password:
        raise CliError(5, f"拒绝非法或带凭据的 URL: {parsed.geturl()!r}")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise CliError(5, f"无法解析主机 {host!r}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            if not ALLOW_PRIVATE:
                raise CliError(
                    5,
                    f"拒绝私网/保留地址 {ip}（本机代理 fake-ip DNS 或内网控制台：设 OBS_ALLOW_PRIVATE=1）",
                )
            print(f"[warn] OBS_ALLOW_PRIVATE 生效，放行 {ip}", file=sys.stderr)
            break
    return str(infos[0][4][0]), port, host


class _PinnedHTTPS(http.client.HTTPSConnection):
    """连到校验过的 IP，但 SNI/Host/证书校验都用真域名（闭合 DNS rebinding 窗口）。"""

    def __init__(self, real_host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(ip, port, timeout=timeout)
        self._real_host = real_host

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self._real_host)


def _connection(scheme: str, ip: str, port: int, real_host: str) -> http.client.HTTPConnection:
    if scheme == "https":
        return _PinnedHTTPS(real_host, ip, port, HTTP_TIMEOUT_S)
    return http.client.HTTPConnection(ip, port, timeout=HTTP_TIMEOUT_S)


def http_raw(method: str, url: str, body: bytes | None = None, *, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """发一次请求（不跟随重定向），返回 (status, headers, body)。5xx/429 重试一次。"""

    if not KEY:
        raise CliError(2, "缺少环境变量 OBS_API_KEY")
    parsed = urllib.parse.urlparse(url)
    ip, port, real_host = _validate_target(parsed)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    attempt = 0
    while True:
        conn = _connection(parsed.scheme, ip, port, real_host)
        try:
            sent = {"Host": real_host, "Authorization": "Bearer " + KEY, "Accept": "*/*"}
            if body is not None:
                sent["Content-Type"] = "application/json"
            if headers:
                sent.update(headers)
            conn.request(method, path, body=body, headers=sent)
            response = conn.getresponse()
            status = response.status
            payload = response.read()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
        except (OSError, http.client.HTTPException) as exc:
            raise CliError(5, f"请求失败: {exc}") from exc
        finally:
            conn.close()
        if status in RETRY_STATUS and attempt == 0:
            attempt += 1
            time.sleep(1.0)
            continue
        if 300 <= status < 400:
            raise CliError(5, f"收到重定向 {status}（已按策略拒绝跟随，防止密钥外送）：{response_headers.get('location', '')}")
        return status, response_headers, payload


def call(method: str, path: str, body: dict | None = None, *, timeout_hint: str = "") -> dict:
    """JSON 端点调用：校验信封 code，返回 data。失败按退出码终止。"""

    payload = json.dumps(body).encode() if body is not None else None
    status, _, raw = http_raw(method, BASE + path, payload)
    if status == 401:
        raise CliError(3, "认证失败（401）：OBS_API_KEY 无效或未设置 api_key")
    if status == 404:
        raise CliError(5, "404：路径或设备不存在（GONE）")
    if status >= 400:
        raise CliError(5, f"HTTP {status}: {raw.decode('utf-8', 'replace')[:400]}")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError(5, f"响应不是合法 JSON: {exc}") from exc
    if isinstance(envelope, dict) and envelope.get("code") not in (None, "OK"):
        raise CliError(5, f"{envelope.get('code')}: {envelope.get('message', '')}")
    return envelope.get("data", envelope) if isinstance(envelope, dict) else envelope


def envelope_time(data: dict) -> int:
    return int(data.get("server_time_ms") or 0)


# --------------------------------------------------------------- 输出助手 ---
def emit(data: object, columns: list[tuple[str, str]] | None = None) -> None:
    """--json 模式输出原始 JSON；否则按 columns 渲染表格（无 columns 时 JSON）。"""

    if JSON_MODE or not columns:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    rows = data if isinstance(data, list) else [data]
    widths = [len(title) for title, _ in columns]
    rendered: list[list[str]] = []
    for row in rows:
        cells = []
        for index, (_, key) in enumerate(columns):
            value = row.get(key, "") if isinstance(row, dict) else ""
            text = "" if value is None else str(value)
            cells.append(text)
            widths[index] = max(widths[index], len(text))
        rendered.append(cells)
    print("  ".join(title.ljust(widths[i]) for i, (title, _) in enumerate(columns)))
    for cells in rendered:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)))


def _uid(value: str) -> str:
    if not value.startswith("OBS-"):
        raise CliError(2, "设备标识应以 OBS- 开头（devices 里查）")
    return value


def _presence(device: dict) -> dict:
    return device.get("presence") or device.get("online") or {}


def _wait_terminal(device: str, command_id: str, budget_s: float) -> dict:
    """轮询到终态；预算耗尽返回最后一帧状态（调用方据此定退出码），不抛异常。"""

    deadline = time.monotonic() + budget_s
    row: dict = {"id": command_id, "status": "pending"}
    while time.monotonic() < deadline:
        row = call("GET", f"/api/v1/devices/{_uid(device)}/commands/{command_id}")
        if str(row.get("status")) in TERMINAL:
            return row
        time.sleep(2.0)
    return row


def _submit(device: str, body: dict) -> tuple[dict, dict]:
    created = call("POST", f"/api/v1/devices/{_uid(device)}/commands", body)
    command_id = str(created.get("id") or created.get("command_id") or "")
    if not command_id:
        raise CliError(5, f"命令创建回执缺少 id: {created}")
    return created, {"id": command_id, "status": created.get("status", "pending")}


# --------------------------------------------------------------- 子命令 ---
def cmd_devices(_args: argparse.Namespace) -> None:
    payload = call("GET", "/api/v1/devices")
    items = payload.get("devices") or payload.get("items") or []
    rows = []
    for item in items:
        presence = _presence(item)
        rows.append(
            {
                "uid": item.get("uid", "?"),
                "name": item.get("name") or item.get("hostname") or "?",
                "state": presence.get("state", "?"),
                "transport": presence.get("transport", "?"),
                "version": item.get("agent_version", "?"),
                "last_seen_age_ms": presence.get("last_seen_ms") and presence.get("age_ms"),
            }
        )
    emit(rows, columns=[("设备", "uid"), ("名称", "name"), ("状态", "state"), ("传输", "transport"), ("版本", "version"), ("最后心跳(ms)", "last_seen_age_ms")])


def cmd_status(args: argparse.Namespace) -> None:
    emit(call("GET", f"/api/v1/devices/{_uid(args.uid)}"))


def cmd_sysinfo(args: argparse.Namespace) -> None:
    created, row = _submit(args.uid, {"type": "sysinfo", "payload": {}})
    row = _wait_terminal(args.uid, str(row["id"]), 90)
    if row.get("status") != "ok":
        print(json.dumps(row, ensure_ascii=False, indent=2))
        raise CliError(4, f"sysinfo 未成功: status={row.get('status')} error={row.get('error')}")
    emit(row.get("result") or {})


def cmd_exec(args: argparse.Namespace) -> None:
    if not args.yes and not args.dry_run:
        raise CliError(2, "exec 以 SYSTEM 权限执行——确认请加 --yes（或先 --dry-run 看请求体）")
    body = {"type": "exec", "payload": {"command": args.command, "timeout_s": max(5, min(600, args.timeout))}, "confirm": True}
    if args.dry_run:
        print(json.dumps({"method": "POST", "path": f"/api/v1/devices/{_uid(args.device)}/commands", "body": body}, ensure_ascii=False, indent=2))
        return
    created, row = _submit(args.device, body)
    if not JSON_MODE:
        print(f"command_id={row['id']} status={row['status']}", file=sys.stderr)
    row = _wait_terminal(args.device, row["id"], min(600, max(30, body["payload"]["timeout_s"] + 15)))
    result = row.get("result") or {}
    if not JSON_MODE:
        if result.get("stdout"):
            print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")
        if result.get("stderr"):
            print("[stderr]", result["stderr"], file=sys.stderr)
        print(f"-- status={row.get('status')} exit={result.get('exit_code')} duration_ms={result.get('duration_ms')}", file=sys.stderr)
    else:
        emit({"command": row, "result": result})
    if row.get("status") == "ok":
        exit_code = int(result.get("exit_code") or 0)
        if exit_code:
            raise CliError(exit_code if 0 < exit_code <= 255 else 4, f"远端退出码 {exit_code}")
        return
    raise CliError(4, f"远端未成功: status={row.get('status')} error={row.get('error')} error_code={row.get('error_code')}")


def cmd_shot(args: argparse.Namespace) -> None:
    """截图并等到**新于下发时刻**的帧：screenshot ok ≠ latest 是新图（A8）。"""

    device = _uid(args.device)
    created, row = _submit(device, {"type": "screenshot", "payload": {}})
    sent_ms = envelope_time(created)
    row = _wait_terminal(device, str(row["id"]), 60)
    if row.get("status") != "ok":
        raise CliError(4, f"截图命令未成功: status={row.get('status')} error={row.get('error')}")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        latest = call("GET", f"/api/v1/devices/{device}/frames/latest")
        frame = latest.get("frame") or {}
        if int(frame.get("ts_ms") or 0) > sent_ms:
            _save_or_print_frame(device, frame, args.out)
            return
        time.sleep(2.0)
    raise CliError(4, f"30s 内没有出现新于下发时刻（{sent_ms}）的帧；可能采集暂停或时间线未入库")


def _save_or_print_frame(device: str, frame: dict, out: str | None) -> None:
    frame_id = frame.get("id")
    summary = {"id": frame_id, "ts_ms": frame.get("ts_ms"), "width": frame.get("width"), "height": frame.get("height")}
    if JSON_MODE or not out:
        print(json.dumps({"frame": summary}, ensure_ascii=False))
        if out:
            print(f"frame_id={frame_id}", file=sys.stderr)
    status, _, payload = http_raw("GET", f"{BASE}/api/v1/devices/{device}/frames/{frame_id}?variant=full")
    if status != 200:
        raise CliError(5, f"下载帧失败 HTTP {status}")
    target = out or f"frame-{frame_id}.webp"
    with open(target, "wb") as handle:
        handle.write(payload)
    if not JSON_MODE:
        print(f"已保存 {target}（{len(payload)} 字节，WebP）", file=sys.stderr)


def cmd_collect(args: argparse.Namespace) -> None:
    created, row = _submit(args.uid, {"type": "collect_now", "payload": {}})
    row = _wait_terminal(args.uid, str(row["id"]), 60)
    raise CliError(0 if row.get("status") == "ok" else 4, f"collect_now status={row.get('status')}")


def cmd_log(args: argparse.Namespace) -> None:
    created, row = _submit(args.device, {"type": "log_tail", "payload": {"role": args.role, "lines": args.lines}})
    row = _wait_terminal(args.device, str(row["id"]), 60)
    if row.get("status") != "ok":
        raise CliError(4, f"log_tail 未成功: {row.get('error')}")
    for line in (row.get("result") or {}).get("lines") or []:
        print(line if isinstance(line, str) else json.dumps(line, ensure_ascii=False))


def cmd_files(args: argparse.Namespace) -> None:
    """/files/op：list/stat/mkdir/delete/rename/copy/move/search/drives/hash（读写走 get/put）。"""

    body: dict = {"op": args.op}
    if args.path:
        body["path"] = args.path
    if args.arg:
        body["to"] = args.arg  # rename/copy/move 的目标；其余 op 忽略
    if args.query:
        body["query"] = args.query
    payload = call("POST", f"/api/v1/devices/{_uid(args.device)}/files/op", body)
    data = payload.get("data") or payload  # 双层 data：envelope.data.data
    if args.op == "list":
        emit(data.get("entries") or [], columns=[("类型", "is_dir"), ("大小", "size"), ("名称", "name")])
        if data.get("truncated"):
            print("-- 列表被截断", file=sys.stderr)
    elif args.op == "drives":
        emit(data.get("drives") or [], columns=[("名称", "name"), ("总空间", "total"), ("可用", "free")])
    elif args.op == "search":
        emit(data.get("entries") or data.get("results") or [], columns=[("路径", "path"), ("大小", "size")])
    else:
        emit(data)


def cmd_get(args: argparse.Namespace) -> None:
    """分片下载：GET /files/raw 直到 X-Eof=1。"""

    device = _uid(args.device)
    out = args.local or os.path.basename(args.remote.replace("\\", "/").rstrip("/")) or "download.bin"
    offset = 0
    total = None
    with open(out, "wb") as handle:
        while True:
            url = f"{BASE}/api/v1/devices/{device}/files/raw?path={urllib.parse.quote(args.remote)}&offset={offset}&length={CHUNK}"
            status, headers, payload = http_raw("GET", url)
            if status != 200:
                raise CliError(5, f"HTTP {status}: {payload.decode('utf-8', 'replace')[:200]}")
            handle.write(payload)
            total = int(headers.get("x-file-size") or total or 0)
            offset += len(payload)
            if headers.get("x-eof") == "1" or not payload:
                break
    print(f"已下载 {out}（{offset}/{total} 字节，{offset // 1024} KiB）", file=sys.stderr)


def cmd_put(args: argparse.Namespace) -> None:
    """分片上传：PUT /files/raw（单片 ≤1MiB，首片可 truncate）。"""

    device = _uid(args.device)
    size = os.path.getsize(args.local)
    with open(args.local, "rb") as handle:
        offset = 0
        first = True
        while True:
            chunk = handle.read(CHUNK)
            if not chunk and not (first and args.truncate):
                break
            url = (
                f"{BASE}/api/v1/devices/{device}/files/raw?path={urllib.parse.quote(args.remote)}"
                f"&offset={offset}&truncate={'true' if (args.truncate and first) else 'false'}"
            )
            status, _, payload = http_raw("PUT", url, chunk, headers={"Content-Type": "application/octet-stream"})
            if status >= 400:
                raise CliError(5, f"HTTP {status}: {payload.decode('utf-8', 'replace')[:200]}")
            offset += len(chunk)
            first = False
            if not chunk:
                break
    print(f"已上传 {args.local} → {args.remote}（{offset}/{size} 字节）", file=sys.stderr)


def cmd_timeline(args: argparse.Namespace) -> None:
    query = f"?from_ms={args.from_ms}&to_ms={args.to_ms}" if args.to_ms else "?from_ms=" + str(args.from_ms)
    emit(call("GET", f"/api/v1/devices/{_uid(args.device)}/timeline{query}"))


def cmd_frames(args: argparse.Namespace) -> None:
    emit(call("GET", f"/api/v1/devices/{_uid(args.device)}/frames?limit={args.limit}"))


def cmd_frame(args: argparse.Namespace) -> None:
    device = _uid(args.device)
    status, _, payload = http_raw("GET", f"{BASE}/api/v1/devices/{device}/frames/{args.frame_id}?variant={args.variant}")
    if status != 200:
        raise CliError(5, f"HTTP {status}")
    target = args.out or f"frame-{args.frame_id}.webp"
    with open(target, "wb") as handle:
        handle.write(payload)
    print(f"已保存 {target}（{len(payload)} 字节）", file=sys.stderr)


def cmd_settings(args: argparse.Namespace) -> None:
    if args.set:
        values: dict = {}
        for pair in args.set:
            key, _, raw = pair.partition("=")
            raw = raw.strip()
            values[key.strip()] = (raw.lower() in {"1", "true", "yes", "on"}) if raw.lower() in {"true", "false"} else _maybe_int(raw)
        emit(call("PATCH", "/api/v1/settings", {"values": values}))
        return
    data = call("GET", "/api/v1/settings")
    values = data.get("values") or data
    emit(values.get(args.key) if args.key else values)


def _maybe_int(raw: str) -> object:
    try:
        return int(raw)
    except ValueError:
        return raw


def cmd_effective(args: argparse.Namespace) -> None:
    emit(call("GET", f"/api/v1/settings/effective/{_uid(args.uid)}"))


def cmd_device_set(args: argparse.Namespace) -> None:
    body: dict = {}
    if args.note is not None:
        body["note"] = args.note
    if args.set:
        body["settings"] = {}
        for pair in args.set:
            key, _, raw = pair.partition("=")
            body["settings"][key.strip()] = (raw.strip().lower() in {"1", "true", "yes", "on"}) if raw.strip().lower() in {"true", "false"} else _maybe_int(raw.strip())
    emit(call("PATCH", f"/api/v1/devices/{_uid(args.uid)}", body))


def cmd_commands(args: argparse.Namespace) -> None:
    payload = call("GET", f"/api/v1/devices/{_uid(args.device)}/commands?limit={args.limit}")
    emit(payload.get("commands") or payload.get("items") or payload, columns=[("状态", "status"), ("类型", "type"), ("id", "id")])


def cmd_wait(args: argparse.Namespace) -> None:
    row = _wait_terminal(args.device, args.command_id, args.timeout)
    emit(row)
    raise CliError(0 if row.get("status") == "ok" else 4, "")


def cmd_audit(args: argparse.Namespace) -> None:
    payload = call("GET", f"/api/v1/audit?limit={args.limit}&offset={args.offset}")
    rows = payload.get("items") or payload.get("entries") or []
    if JSON_MODE:
        emit(rows)
        return
    for item in rows:
        print(f"{item.get('ts_ms', '?')} {item.get('actor', '?'):10} {item.get('action', '?'):26} {item.get('device_uid') or '-':18} {item.get('ip') or '-'}")


def _dangerous(kind: str, args: argparse.Namespace) -> None:
    if not args.yes:
        raise CliError(2, f"{kind} 是破坏性操作——确认请加 --yes")
    created, row = _submit(args.uid, {"type": kind, "payload": {}})
    row = _wait_terminal(args.uid, str(row["id"]), 90)
    raise CliError(0 if row.get("status") == "ok" else 4, f"{kind} status={row.get('status')} error={row.get('error')}")


def cmd_reboot(args: argparse.Namespace) -> None:
    _dangerous("reboot", args)


def cmd_poweroff(args: argparse.Namespace) -> None:
    _dangerous("poweroff", args)


def cmd_restart_agent(args: argparse.Namespace) -> None:
    _dangerous("restart_agent", args)


def cmd_uninstall(args: argparse.Namespace) -> None:
    _dangerous("uninstall", args)


def cmd_reload_config(args: argparse.Namespace) -> None:
    created, row = _submit(args.uid, {"type": "reload_config", "payload": {}})
    row = _wait_terminal(args.uid, str(row["id"]), 60)
    raise CliError(0 if row.get("status") == "ok" else 4, f"reload_config status={row.get('status')}")


def cmd_raw(args: argparse.Namespace) -> None:
    body = json.loads(args.body) if args.body else None
    status, headers, payload = http_raw(args.method.upper(), BASE + args.path, json.dumps(body).encode() if body is not None else None)
    content_type = headers.get("content-type", "")
    if "json" in content_type:
        print(json.dumps(json.loads(payload.decode("utf-8")), ensure_ascii=False, indent=2))
        raise CliError(0 if status < 400 else 5, "")
    target = args.out
    if not target:
        raise CliError(2, f"响应是二进制（{content_type or 'unknown'}，{len(payload)} 字节）——用 --out 保存到文件")
    with open(target, "wb") as handle:
        handle.write(payload)
    print(f"已保存 {target}（{len(payload)} 字节）", file=sys.stderr)
    raise CliError(0 if status < 400 else 5, "")


# --------------------------------------------------------------- 装配 ---
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--allow-private", action="store_true", help="放行私网/环回地址（也可用环境变量 OBS_ALLOW_PRIVATE=1）")
    common.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    parser = argparse.ArgumentParser(description="Observatory 控制台 CLI（退出码：0 成功/3 认证/4 远端失败/5 网络服务端）", parents=[common])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name: str, **kwargs: object) -> argparse.ArgumentParser:
        child = sub.add_parser(name, parents=[common], **kwargs)
        return child

    add("devices").set_defaults(func=cmd_devices)
    p = add("status"); p.add_argument("uid"); p.set_defaults(func=cmd_status)
    p = add("sysinfo"); p.add_argument("uid"); p.set_defaults(func=cmd_sysinfo)

    p = add("exec")
    p.add_argument("device"); p.add_argument("command")
    p.add_argument("--timeout", type=int, default=60, help="远端超时 5..600 秒（服务端钳制）")
    p.add_argument("--yes", action="store_true", help="确认以 SYSTEM 权限执行")
    p.add_argument("--dry-run", action="store_true", help="只打印请求体不发送")
    p.set_defaults(func=cmd_exec)

    p = add("shot"); p.add_argument("device"); p.add_argument("--out", default=""); p.set_defaults(func=cmd_shot)
    p = add("collect"); p.add_argument("uid"); p.set_defaults(func=cmd_collect)

    p = add("log")
    p.add_argument("device"); p.add_argument("--role", default="agent", choices=["agent", "worker-1", "update"]); p.add_argument("--lines", type=int, default=200)
    p.set_defaults(func=cmd_log)

    p = add("files")
    p.add_argument("device")
    p.add_argument("op", choices=["list", "stat", "mkdir", "delete", "rename", "copy", "move", "search", "drives", "hash"], help="读写用 get/put 子命令")
    p.add_argument("path", nargs="?"); p.add_argument("arg", nargs="?"); p.add_argument("--query", default="")
    p.set_defaults(func=cmd_files)

    p = add("get"); p.add_argument("device"); p.add_argument("remote"); p.add_argument("local", nargs="?"); p.set_defaults(func=cmd_get)
    p = add("put"); p.add_argument("device"); p.add_argument("local"); p.add_argument("remote"); p.add_argument("--truncate", action="store_true"); p.set_defaults(func=cmd_put)

    p = add("timeline"); p.add_argument("device"); p.add_argument("--from-ms", type=int, default=0); p.add_argument("--to-ms", type=int, default=0); p.set_defaults(func=cmd_timeline)
    p = add("frames"); p.add_argument("device"); p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_frames)
    p = add("frame"); p.add_argument("device"); p.add_argument("frame_id"); p.add_argument("--variant", default="full", choices=["full", "thumb"]); p.add_argument("--out", default=""); p.set_defaults(func=cmd_frame)

    p = add("settings"); p.add_argument("key", nargs="?"); p.add_argument("--set", nargs="*", help="KEY=VALUE …（true/false 自动转布尔）"); p.set_defaults(func=cmd_settings)
    p = add("effective"); p.add_argument("uid"); p.set_defaults(func=cmd_effective)
    p = add("device-set"); p.add_argument("uid"); p.add_argument("--note", default=None); p.add_argument("--set", nargs="*", help="per-device 覆盖 KEY=VALUE"); p.set_defaults(func=cmd_device_set)

    p = add("commands"); p.add_argument("device"); p.add_argument("--limit", type=int, default=25); p.set_defaults(func=cmd_commands)
    p = add("wait"); p.add_argument("command_id"); p.add_argument("--device", required=True); p.add_argument("--timeout", type=float, default=600); p.set_defaults(func=cmd_wait)
    p = add("audit"); p.add_argument("--limit", type=int, default=20); p.add_argument("--offset", type=int, default=0); p.set_defaults(func=cmd_audit)

    for name, func in (("reboot", cmd_reboot), ("poweroff", cmd_poweroff), ("restart-agent", cmd_restart_agent), ("uninstall", cmd_uninstall)):
        p = add(name); p.add_argument("uid"); p.set_defaults(func=func)
    p = add("reload-config"); p.add_argument("uid"); p.set_defaults(func=cmd_reload_config)

    p = add("raw"); p.add_argument("method"); p.add_argument("path"); p.add_argument("body", nargs="?"); p.add_argument("--out", default=""); p.set_defaults(func=cmd_raw)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # GBK 控制台防乱码
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    global ALLOW_PRIVATE, JSON_MODE
    ALLOW_PRIVATE = ALLOW_PRIVATE or bool(args.allow_private)
    JSON_MODE = bool(args.json)
    if ALLOW_PRIVATE:
        print("[warn] 已放行私网/保留地址（仅限本机代理 fake-ip 或内网控制台场景）", file=sys.stderr)
    try:
        args.func(args)
    except CliError as exc:
        if exc.code:
            print(f"错误: {exc.message}" if exc.message else "错误", file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
