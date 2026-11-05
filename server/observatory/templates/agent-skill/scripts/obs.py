#!/usr/bin/env python3
"""Observatory 控制台 CLI——本地 agent 管理远端设备的薄封装。

环境变量：
    OBS_API_BASE   控制台地址（默认 https://control.tianhw.top）
    OBS_API_KEY    Bearer 密钥（必填）

用法：
    python obs.py devices
    python obs.py status OBS-xxxx
    python obs.py exec OBS-xxxx "whoami" [--timeout 60] [--yes]
    python obs.py shot OBS-xxx              # 截图并等终态
    python obs.py files OBS-xxx list C:\\    # op: list/stat/mkdir/delete/rename/copy/move/search/drives/read/write/hash
    python obs.py log OBS-xxx [--role agent] [--lines 200]
    python obs.py audit [--limit 20]
    python obs.py wait <command_id> --device OBS-xxx
    python obs.py raw GET /devices          # 透传任意端点（全自动认证）
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("OBS_API_BASE", "https://@@CONSOLE_HOST@@").rstrip("/")
KEY = os.environ.get("OBS_API_KEY", "")
TERMINAL = {"ok", "error", "timeout", "cancelled", "failed", "unconfirmed"}
_ALLOW_PRIVATE = False


def _validated(url: str) -> str:
    """请求边界：仅 http/https，拒绝私网/环回/链路本地/保留地址（--allow-private 出口）。"""

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SystemExit(f"拒绝请求非法 URL: {url!r}")
    if parsed.username or parsed.password:
        raise SystemExit("拒绝带凭据的 URL")
    if _ALLOW_PRIVATE:
        return url
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SystemExit(f"无法解析主机 {host!r}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise SystemExit(f"拒绝请求私网/保留地址 {ip}（内网控制台加 --allow-private）")
    return url


def call(method: str, path: str, body: dict | None = None, *, timeout: float = 30.0) -> dict:
    if not KEY:
        raise SystemExit("缺少环境变量 OBS_API_KEY")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_validated(BASE + path), data=data, method=method)
    req.add_header("Authorization", "Bearer " + KEY)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {exc.code}: {detail[:400]}") from exc
    if not payload:
        return {}
    envelope = json.loads(payload)
    return envelope.get("data", envelope)


def _uid_argument(value: str) -> str:
    if not value.startswith("OBS-"):
        raise SystemExit("设备标识应以 OBS- 开头（devices 里查）")
    return value


def cmd_devices(_args: argparse.Namespace) -> None:
    payload = call("GET", "/api/v1/devices")
    items = payload.get("devices") or payload.get("items") or []
    for item in items:
        print(
            f"{item.get('uid','?'):20} {item.get('name') or item.get('hostname') or '?':24} "
            f"online={str(item.get('online', item.get('presence','?'))):5} "
            f"ver={item.get('agent_version','?'):8} last_seen={item.get('last_seen_ms','?')}"
        )
    print(f"-- 共 {len(items)} 台")


def cmd_status(args: argparse.Namespace) -> None:
    print(json.dumps(call("GET", f"/api/v1/devices/{_uid_argument(args.uid)}"), ensure_ascii=False, indent=2))


def _submit_exec(args: argparse.Namespace) -> dict:
    body = {"type": "exec", "payload": {"command": args.command, "timeout_s": args.timeout}, "confirm": True}
    return call("POST", f"/api/v1/devices/{_uid_argument(args.device)}/commands", body)


def _wait_terminal(device: str, command_id: str, budget_s: float) -> dict:
    path = f"/api/v1/devices/{_uid_argument(device)}/commands/{command_id}"
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        row = call("GET", path)
        if str(row.get("status")) in TERMINAL:
            return row
        time.sleep(2.0)
    raise SystemExit(f"命令 {command_id} 在 {budget_s:.0f}s 内未到终态，稍后用 wait 子命令再查")


def cmd_exec(args: argparse.Namespace) -> None:
    if not args.yes:
        raise SystemExit("exec 以 SYSTEM 权限执行——确认无误请加 --yes")
    created = _submit_exec(args)
    command_id = created.get("id") or created.get("command_id")
    print(f"command_id={command_id} status={created.get('status')}", file=sys.stderr)
    row = _wait_terminal(args.device, str(command_id), args.timeout + 15)
    result = row.get("result") or {}
    if result.get("stdout"):
        print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")
    if result.get("stderr"):
        print("[stderr]", result["stderr"], file=sys.stderr)
    print(
        f"-- status={row.get('status')} exit={result.get('exit_code')} "
        f"duration_ms={result.get('duration_ms')} timed_out={result.get('timed_out')}",
        file=sys.stderr,
    )


def cmd_shot(args: argparse.Namespace) -> None:
    device = _uid_argument(args.device)
    created = call("POST", f"/api/v1/devices/{device}/commands", {"type": "screenshot", "payload": {}})
    command_id = created.get("id") or created.get("command_id")
    row = _wait_terminal(device, str(command_id), 60)
    print(f"截图命令终态 status={row.get('status')}")
    latest = call("GET", f"/api/v1/devices/{device}/frames/latest")
    frame = latest.get("frame") or {}
    print(json.dumps(frame, ensure_ascii=False, indent=2))
    if frame.get("id"):
        print(f"下载全图：GET {BASE}/api/v1/devices/{device}/frames/{frame['id']}?variant=full")


def cmd_files(args: argparse.Namespace) -> None:
    body: dict = {"op": args.op}
    if args.path:
        body["path"] = args.path
    if args.arg:
        body.setdefault("to", args.arg)
    payload = call("POST", f"/api/v1/devices/{_uid_argument(args.device)}/files/op", body)
    data = payload.get("data") or {}
    for entry in data.get("entries") or []:
        kind = "d" if entry.get("is_dir") else "-"
        print(f"{kind} {entry.get('size', ''):>12} {entry.get('name')}")
    if data.get("truncated"):
        print("-- 列表被截断")


def cmd_log(args: argparse.Namespace) -> None:
    body = {"type": "log_tail", "payload": {"role": args.role, "lines": args.lines}}
    created = call("POST", f"/api/v1/devices/{_uid_argument(args.device)}/commands", body)
    command_id = created.get("id") or created.get("command_id")
    row = _wait_terminal(args.device, str(command_id), 60)
    for line in (row.get("result") or {}).get("lines") or []:
        print(line if isinstance(line, str) else json.dumps(line, ensure_ascii=False))


def cmd_audit(args: argparse.Namespace) -> None:
    rows = call("GET", f"/api/v1/audit?limit={args.limit}")
    items = rows.get("items") or rows.get("entries") or []
    for item in items:
        print(
            f"{item.get('ts_ms','?')} {item.get('actor','?'):10} {item.get('action','?'):24} "
            f"{item.get('device_uid') or '-':18} {item.get('ip') or '-'}"
        )


def cmd_wait(args: argparse.Namespace) -> None:
    row = _wait_terminal(args.device, args.command_id, args.timeout)
    print(json.dumps(row, ensure_ascii=False, indent=2))


def cmd_raw(args: argparse.Namespace) -> None:
    body = json.loads(args.body) if args.body else None
    print(json.dumps(call(args.method.upper(), args.path, body), ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observatory 控制台 CLI")
    parser.add_argument("--allow-private", action="store_true", help="允许内网/环回地址（内网控制台）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices").set_defaults(func=cmd_devices)

    p = sub.add_parser("status"); p.add_argument("uid"); p.set_defaults(func=cmd_status)

    p = sub.add_parser("exec")
    p.add_argument("device"); p.add_argument("command")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--yes", action="store_true", help="确认以 SYSTEM 权限执行")
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("shot"); p.add_argument("device"); p.set_defaults(func=cmd_shot)

    p = sub.add_parser("files")
    p.add_argument("device"); p.add_argument("op"); p.add_argument("path", nargs="?"); p.add_argument("arg", nargs="?")
    p.set_defaults(func=cmd_files)

    p = sub.add_parser("log")
    p.add_argument("device"); p.add_argument("--role", default="agent"); p.add_argument("--lines", type=int, default=200)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("audit"); p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_audit)

    p = sub.add_parser("wait"); p.add_argument("command_id"); p.add_argument("--device", required=True)
    p.add_argument("--timeout", type=float, default=600)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("raw"); p.add_argument("method"); p.add_argument("path"); p.add_argument("body", nargs="?")
    p.set_defaults(func=cmd_raw)

    args = parser.parse_args(argv)
    global _ALLOW_PRIVATE
    _ALLOW_PRIVATE = bool(args.allow_private)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
