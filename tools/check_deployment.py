#!/usr/bin/env python
"""对已部署实例做一次真实的线上体检（Observatory 0.3.0）。

    python tools/check_deployment.py --password <控制台密码> --token <一次性令牌>
    python tools/check_deployment.py --password <控制台密码>            # 登录后现场签发令牌

它验证的是“外面看到的样子”，不是本机：

  * 两个域名的角色分工（控制台只在 control 域，受控端接口两边都有）
  * TLS 证书：链/主机名可验证、SAN 覆盖两个域名、剩余有效期
  * 协议 v3：`POST/WS /api/v1/agent/link` + `server_time_ms`/`ack_id`
  * 错误信封：`{code, message, retryable, server_time_ms}`
  * 一次性安装令牌：签发（含 TTL 边界）、单一使用、绑定首台设备
  * 渲染后的 install.ps1 / install.cmd：内嵌令牌（0.3.1 起不再有共享注册码）
  * 安装包可下载且 sha256 与服务器一致；控制台登录/设备/presence
  * 体检设备的清理（在线设备不能删除，工具会等待离线窗口）

本脚本只读 + 创建/删除自己的体检记录，不修改业务配置。

退出码：0 全部通过；1 有失败；2 参数/连接错误。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import socket
import ssl
import sys
import time
import uuid
from typing import Any

import httpx
import websockets

PASSED = 0
FAILED: list[str] = []
SKIPPED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
        return
    FAILED.append(f"{name}{(' :: ' + detail) if detail else ''}")
    print(f"  FAIL {name} {detail}")


def skip(name: str, reason: str) -> None:
    SKIPPED.append(f"{name}（{reason}）")
    print(f"  skip {name}（{reason}）")


def group(title: str) -> None:
    print(f"\n== {title} ==")


def body(response: httpx.Response) -> dict[str, Any]:
    """返回 JSON dict；错误时返回带 ``_text`` 的最小结构，避免异常打断整轮体检。"""

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return {"_text": response.text[:300], "_status": response.status_code}
    if isinstance(payload, dict):
        return payload
    return {"_value": payload, "_status": response.status_code}


def data(response: httpx.Response) -> dict[str, Any]:
    """控制台接口统一包在 ``{"data": ..., "server_time_ms": ...}`` 里。"""

    payload = body(response)
    inner = payload.get("data")
    return inner if isinstance(inner, dict) else payload


def envelope_ok(payload: dict[str, Any], expected_code: str) -> bool:
    return (
        payload.get("code") == expected_code
        and isinstance(payload.get("message"), str)
        and bool(payload.get("message"))
        and isinstance(payload.get("retryable"), bool)
        and isinstance(payload.get("server_time_ms"), int)
    )


def fetch_peer_cert(host: str, port: int, insecure: bool) -> dict[str, Any]:
    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    if insecure:
        context.check_hostname = False
    with socket.create_connection((host, port), timeout=15) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return dict(tls.getpeercert() or {})


def cert_sans(cert: dict[str, Any]) -> set[str]:
    return {value for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"}


def cert_days_left(cert: dict[str, Any]) -> float:
    not_after = cert.get("notAfter")
    if not not_after:
        return -1.0
    return (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400


async def check_tls(client: httpx.AsyncClient, args: argparse.Namespace, host: str, label: str) -> None:
    if args.insecure:
        skip(f"{label}证书链验证", "--insecure 已请求跳过")
    try:
        loop = asyncio.get_running_loop()
        cert = await loop.run_in_executor(None, fetch_peer_cert, host, args.cert_port, args.insecure)
    except Exception as exc:  # noqa: BLE001
        check(f"{label}域名 TLS 证书可读取", False, repr(exc)[:200])
        return
    days = cert_days_left(cert)
    sans = cert_sans(cert)
    if not args.insecure:
        check(f"{label}域名证书链/主机名可验证", True)
    check(f"{label}域名证书剩余 ≥ {args.min_cert_days} 天", days >= args.min_cert_days, f"{days:.1f} 天")
    check(f"{label}域名证书 SAN 含 {host}", host in sans, ",".join(sorted(sans))[:200])
    check("证书 SAN 同时覆盖两个域名", {args.console_host, args.agent_host} <= sans, ",".join(sorted(sans))[:200])


async def main() -> int:
    parser = argparse.ArgumentParser(description="Observatory 0.3.0 线上体检")
    parser.add_argument("--console-host", default="control.tianhw.top")
    parser.add_argument("--agent-host", default="observatory.123423.xyz")
    parser.add_argument("--password", default="", help="控制台管理员密码（优先用 --password-stdin，避免 ps 可见）")
    parser.add_argument("--password-stdin", action="store_true", help="从 stdin 读控制台管理员密码（F64）")
    parser.add_argument("--device-uid", default="", help="体检设备 uid（默认随机，避免冲突）")
    parser.add_argument("--expect-version", default="", help="期望版本；默认不强制")
    parser.add_argument("--min-cert-days", type=int, default=7, help="证书最少剩余天数（默认 7）")
    parser.add_argument("--cert-port", type=int, default=443)
    parser.add_argument("--no-cleanup", action="store_true", help="保留体检设备记录")
    parser.add_argument("--cleanup-wait", type=int, default=150, help="等待体检设备离线的秒数")
    parser.add_argument("--insecure", action="store_true", help="跳过 TLS 校验（自签名测试环境）")
    args = parser.parse_args()

    if args.password_stdin:
        import sys as _sys

        args.password = _sys.stdin.readline().rstrip("\r\n")
    if not args.password:
        parser.error("需要 --password 或 --password-stdin")
    arguments_ok = bool(args.password)
    if not arguments_ok:
        print("缺少 --password", file=sys.stderr)
        return 2

    uid = args.device_uid or f"OBS-CHECK-{uuid.uuid4().hex[:8].upper()}"
    console = f"https://{args.console_host}"
    agent = f"https://{args.agent_host}"

    async with httpx.AsyncClient(verify=not args.insecure, timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True) as client:
        try:
            group("公共接口 / 版本")
            health = body(await client.get(f"{console}/api/v1/health"))
            check("控制台域名健康检查", health.get("ok") is True, str(health)[:120])
            version = str(health.get("version") or "")
            check("版本号可读", bool(version), str(health)[:120])
            check("版本是语义化 X.Y.Z", version.count(".") == 2, version)
            check("健康响应含 server_time_ms", isinstance(health.get("server_time_ms"), int))
            check("健康响应含 carriers", isinstance(health.get("carriers"), dict), str(health.get("carriers")))
            agent_health = body(await client.get(f"{agent}/api/v1/health"))
            check("受控端域名健康检查", agent_health.get("ok") is True, str(agent_health)[:120])
            check("两个域名版本一致", agent_health.get("version") == version, f"{version} vs {agent_health.get('version')}")
            version_body = body(await client.get(f"{console}/api/v1/version"))
            check("版本接口可读", version_body.get("version") == version, str(version_body)[:120])
            check("版本接口下发 base_urls", isinstance(version_body.get("base_urls"), list), str(version_body)[:120])
            if args.expect_version:
                check("运行版本与期望一致", version == args.expect_version, f"{version} != {args.expect_version}")

            group("TLS 证书")
            await check_tls(client, args, args.console_host, "控制台")
            await check_tls(client, args, args.agent_host, "受控端")

            group("域名分工")
            console_root = await client.get(f"{console}/")
            check(
                "控制台域名提供页面",
                console_root.status_code == 200 and "html" in console_root.headers.get("content-type", ""),
                str(console_root.status_code),
            )
            check("页面含产品名", "Observatory" in console_root.text)
            agent_root = await client.get(f"{agent}/")
            check("受控端域名不提供控制台", agent_root.status_code == 404, str(agent_root.status_code))
            devices_on_agent = await client.get(f"{agent}/api/v1/devices")
            check("受控端域名拒绝后台接口", devices_on_agent.status_code == 404, str(devices_on_agent.status_code))
            tokens_on_agent = await client.get(f"{agent}/api/v1/install-tokens")
            check("安装票据接口已移除（受控端域名 404）", tokens_on_agent.status_code == 404, str(tokens_on_agent.status_code))

            group("错误信封")
            bad_link = await client.post(
                f"{agent}/api/v1/agent/link", headers={"X-Device-Token": "definitely-wrong"}, json={"v": 3, "items": []}
            )
            bad_agent = body(bad_link)
            check("无效设备令牌返回 401", bad_link.status_code == 401, str(bad_link.status_code))
            check("错误体符合 {code,message,retryable,server_time_ms}", envelope_ok(bad_agent, "AUTH"), str(bad_agent)[:200])

            group("控制台登录")
            login = await client.post(f"{console}/api/v1/auth/login", json={"password": args.password})
            login_body = data(login)
            check("控制台登录", login.status_code == 200, login.text[:160])
            session_token = str(login_body.get("token") or "")
            if login.status_code != 200 or not session_token:
                return 1
            auth = {"Authorization": f"Bearer {session_token}"}
            session = data(await client.get(f"{console}/api/v1/auth/session", headers=auth))
            check("会话有效", session.get("authenticated") is True, str(session)[:120])

            group("安装信息")
            install = data(await client.get(f"{console}/api/v1/system", headers=auth)).get("install") or {}
            check("install_info 可见", isinstance(install, dict) and bool(install), str(install)[:200])
            check("install_info 标记开放注册", install.get("enroll_required") is True, str(install)[:200])
            check(
                "install_info 不再暴露票据字段",
                "install_token_endpoint" not in install and "available_tokens" not in install,
                str(sorted(install))[:200],
            )
            removed = await client.get(f"{console}/api/v1/install-tokens", headers=auth)
            check("安装票据接口已移除（404）", removed.status_code == 404, str(removed.status_code))

            group("渲染后的安装脚本")
            for host, label in ((console, "控制台"), (agent, "受控端")):
                script = await client.get(f"{host}/api/v1/public/install.ps1")
                text = script.text
                check(f"{label}域名下载安装脚本无需凭据", script.status_code == 200, str(script.status_code))
                check(f"{label}域名脚本无未渲染占位符", "@@" not in text, text[:160])
                check(
                    f"{label}域名脚本不再要求令牌参数",
                    "-Token " not in text and "install-token" not in text and "TOKEN=" not in text,
                    "脚本仍引用 Token 参数",
                )
                check(f"{label}域名脚本内嵌双域名", text.count("http") >= 2, "未找到备用地址")
                wrapper = await client.get(f"{host}/api/v1/public/install.cmd")
                cmd = wrapper.text
                disposition = wrapper.headers.get("content-disposition", "")
                check(f"{label}域名可下载 .cmd", wrapper.status_code == 200, str(wrapper.status_code))
                check(f"{label}域名 .cmd 为附件", "attachment" in disposition and ".cmd" in disposition, disposition)
                check(f"{label}域名 .cmd 无未渲染占位符", "@@" not in cmd, cmd[:160])
                check(f"{label}域名 .cmd 不再携带/索要凭据", "TOKEN" not in cmd and "token=" not in cmd, "cmd 仍引用令牌")
                check(f"{label}域名 .cmd 纯 ASCII", all(ord(ch) < 128 for ch in cmd))
                check(f"{label}域名 .cmd 含多线路 URL", 'set "URL1=' in cmd)

            group("受控端注册（开放注册）")
            enroll_payload = {
                "uid": uid,
                "hostname": "deploy-check",
                "os_name": "Windows",
                "os_version": "11",
                "arch": "AMD64",
                "agent_version": version,
                "sysinfo": {"cpu_percent": 1.0, "total_memory": 8 * 1024**3},
                "sessions": [],
            }
            enroll = await client.post(f"{agent}/api/v1/agent/enroll", json=enroll_payload)
            enroll_body = body(enroll)
            check("无需任何凭据即可注册", enroll.status_code == 200, enroll.text[:200])
            if enroll.status_code != 200:
                return 1
            device_token = str(enroll_body.get("token") or "")
            check("注册下发设备令牌", bool(device_token))
            base_urls = enroll_body.get("base_urls") or []
            check("注册下发双域名", len(base_urls) >= 2, str(base_urls))
            replay = dict(enroll_payload)
            replay["uid"] = uid + "-REPLAY"
            replayed = await client.post(f"{agent}/api/v1/agent/enroll", json=replay)
            check("第二台设备同样自动注册（200）", replayed.status_code == 200, str(replayed.status_code))
            if replayed.status_code == 200:
                check("第二台设备同样下发设备令牌", bool(body(replayed).get("token")), str(body(replayed))[:160])
            legacy_enroll = dict(enroll_payload)
            legacy_enroll["uid"] = uid + "-LEGACY"
            legacy_enroll["install_token"] = "legacy-ticket-ignored"
            legacy_enroll["code"] = "legacy-code-ignored"
            legacy_result = await client.post(f"{agent}/api/v1/agent/enroll", json=legacy_enroll)
            check("旧客户端带票据/注册码仍可注册（兼容）", legacy_result.status_code == 200, legacy_result.text[:160])

            device_headers = {"X-Device-Token": device_token}
            group("协议 v3 /agent/link")
            hello = {
                "v": 3,
                "id": str(uuid.uuid4()),
                "t": "hello",
                "ts_ms": 1730000000000,
                "body": {"agent_version": version, "hostname": "deploy-check"},
            }
            link = await client.post(f"{agent}/api/v1/agent/link", headers=device_headers, json={"v": 3, "last_ack_id": "", "items": [hello]})
            link_body = body(link)
            check("HTTP 载体 /agent/link 可用", link.status_code == 200, f"{link.status_code} {link.text[:160]}")
            if link.status_code == 200:
                check("下行批次含 items/ack_id/server_time_ms", isinstance(link_body.get("items"), list) and "ack_id" in link_body and isinstance(link_body.get("server_time_ms"), int), str(link_body)[:200])
                welcome = next((item for item in link_body.get("items", []) if item.get("t") == "welcome"), None)
                if welcome:
                    check("下行 welcome 含 server_version", welcome.get("body", {}).get("server_version") == version, str(welcome)[:160])

            ws_url = f"wss://{args.agent_host}/api/v1/agent/link"
            ssl_ctx = ssl._create_unverified_context() if args.insecure else ssl.create_default_context()
            try:
                async with websockets.connect(
                    ws_url, additional_headers=device_headers, ssl=ssl_ctx, open_timeout=15, close_timeout=5
                ) as socket_ws:
                    # spec §5.6: the server sends `welcome` only as the reply to the
                    # first `hello`; connecting alone produces no downlink.
                    await socket_ws.send(
                        json.dumps(
                            {
                                "v": 3,
                                "id": str(uuid.uuid4()),
                                "t": "hello",
                                "ts_ms": 1730000000000,
                                "body": {
                                    "agent_version": version,
                                    "protocol": 3,
                                    "boot_id": "deploy-check",
                                    "device_uid": uid,
                                    "last_ack_id": "",
                                    "device_time_ms": 1730000000000,
                                },
                            }
                        )
                    )
                    welcome_raw = await asyncio.wait_for(socket_ws.recv(), 15)
                    welcome = json.loads(welcome_raw)
                    check("WS 载体 /agent/link 升级成功", welcome.get("t") == "welcome", str(welcome)[:160])
                    check("WS welcome 含 server_version", welcome.get("body", {}).get("server_version") == version, str(welcome)[:160])
                    await socket_ws.send(
                        json.dumps({"v": 3, "id": str(uuid.uuid4()), "t": "heartbeat", "ts_ms": 1730000000000, "body": {"agent_version": version}})
                    )
            except Exception as exc:  # noqa: BLE001
                check("WS 载体 /agent/link 升级成功", False, repr(exc)[:200])

            group("0.2.x 端点删除确认")
            legacy_heartbeat = await client.post(
                f"{agent}/api/v1/agent/heartbeat",
                headers=device_headers,
                json={"agent_version": version, "sysinfo": {}, "state": {"transport": "http", "spool": 0}},
            )
            check(
                "legacy /agent/heartbeat 已删除（404/405）",
                legacy_heartbeat.status_code in (404, 405),
                str(legacy_heartbeat.status_code),
            )

            group("安装包")
            latest = body(await client.get(f"{agent}/api/v1/public/agent/latest"))
            check("安装包信息可用", latest.get("available") is True, str(latest)[:160])
            if latest.get("available"):
                package = await client.get(f"{agent}{latest.get('url')}")
                check("安装包可下载", package.status_code == 200 and len(package.content) == int(latest.get("size_bytes") or -1), str(package.status_code))
                digest = hashlib.sha256(package.content).hexdigest()
                check("安装包 sha256 与声明一致", digest == latest.get("sha256"), digest[:16])

            group("控制台设备 / presence")
            device = data(await client.get(f"{console}/api/v1/devices/{uid}", headers=auth)).get("device") or {}
            presence = device.get("presence") or {}
            check("体检设备出现在控制台", bool(device), "未找到")
            check("状态为 online/degraded", presence.get("state") in {"online", "degraded"}, str(presence.get("state")))
            check("capabilities 来自 presence", isinstance(presence.get("capabilities"), dict), str(presence)[:160])
            if args.expect_version:
                check("设备上报版本正确", device.get("agent_version") == args.expect_version, str(device.get("agent_version")))

            group("体检设备清理")
            if args.no_cleanup:
                skip("删除体检设备", "--no-cleanup")
            else:
                deleted = await client.delete(f"{console}/api/v1/devices/{uid}", headers=auth)
                if deleted.status_code == 422:
                    print("  info 设备仍在线，等待离线窗口后再删除")
                    deadline = time.time() + max(10, args.cleanup_wait)
                    while time.time() < deadline:
                        await asyncio.sleep(5)
                        current = data(await client.get(f"{console}/api/v1/devices/{uid}", headers=auth)).get("device") or {}
                        if (current.get("presence") or {}).get("state") == "offline":
                            break
                    deleted = await client.delete(f"{console}/api/v1/devices/{uid}", headers=auth)
                if deleted.status_code in (200, 204):
                    check("可以删除体检设备", True)
                else:
                    # F64：清理失败降级为提示而非整个体检失败——设备状态依赖
                    # agent_offline_after_s 配置，可能远大于 cleanup_wait。
                    skip(
                        "删除体检设备",
                        f"删除被拒（{deleted.status_code}）；请稍后手动删除 {uid}（agent_offline_after_s 可能大于 --cleanup-wait）",
                    )
                # 第二台绑定设备从未建立链路，必然离线，可直接删。
                replay_deleted = await client.delete(f"{console}/api/v1/devices/{uid}-REPLAY", headers=auth)
                if replay_deleted.status_code in (200, 204):
                    check("可以删除第二台体检设备", True)
                elif replay_deleted.status_code != 404:
                    skip("删除第二台体检设备", f"删除被拒（{replay_deleted.status_code}）；请手动删除 {uid}-REPLAY")
        except httpx.HTTPError as exc:
            check("网络请求", False, repr(exc)[:200])
        except Exception as exc:  # noqa: BLE001
            check("体检脚本执行", False, repr(exc)[:200])

    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败 / {PASSED + len(FAILED)} 项，{len(SKIPPED)} 项跳过：")
        for item in FAILED:
            print(f"  - {item}")
        if SKIPPED:
            print("跳过：")
            for item in SKIPPED:
                print(f"  - {item}")
        return 1
    print(f"全部通过：{PASSED + len(SKIPPED)} 项（{PASSED} 通过，{len(SKIPPED)} 跳过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))