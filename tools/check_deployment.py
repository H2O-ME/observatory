#!/usr/bin/env python
"""对已部署实例做一次真实的线上体检。

    python tools/check_deployment.py --password <控制台密码> --code <注册码>

它验证的是"外面看到的样子"，不是本机：
  * 两个域名的角色分工（控制台只在 control 域，受控端接口两边都有）
  * 控制台登录、会话、设备接口
  * 受控端注册、WebSocket 升级（两条域名的代理都要支持 upgrade）
  * 安装脚本的版本号与注册码校验
  * 安装包可下载且 sha256 与服务器一致

默认域名可以通过 --console-host / --agent-host 覆盖。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys

import httpx
import websockets

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
        return
    FAILED.append(f"{name}{(' :: ' + detail) if detail else ''}")
    print(f"  FAIL {name} {detail}")


def group(title: str) -> None:
    print(f"\n== {title} ==")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--console-host", default="control.tianhw.top")
    parser.add_argument("--agent-host", default="observatory.123423.xyz")
    parser.add_argument("--password", required=True)
    parser.add_argument("--code", required=True)
    parser.add_argument("--device-uid", default="OBS-DEPLOYCHECK01")
    args = parser.parse_args()

    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        console = f"https://{args.console_host}"
        agent = f"https://{args.agent_host}"

        group("公共接口")
        health = (await client.get(f"{console}/api/v1/health")).json()
        version = health.get("version", "")
        check("控制台域名健康检查", health.get("ok") is True)
        check("版本号可读", bool(version), str(health))
        agent_health = (await client.get(f"{agent}/api/v1/health")).json()
        check("受控端域名健康检查", agent_health.get("ok") is True)
        check("两个域名版本一致", agent_health.get("version") == version, f"{version} vs {agent_health.get('version')}")

        group("域名分工")
        console_root = await client.get(f"{console}/")
        check("控制台域名提供页面", console_root.status_code == 200 and "html" in console_root.headers.get("content-type", ""), str(console_root.status_code))
        check("页面含产品名", "Observatory" in console_root.text)
        agent_root = await client.get(f"{agent}/")
        check("受控端域名不提供控制台", agent_root.status_code == 404, str(agent_root.status_code))

        devices_open = await client.get(f"{agent}/api/v1/devices")
        check("受控端域名拒绝后台接口", devices_open.status_code == 404, str(devices_open.status_code))

        for host, label in ((console, "控制台"), (agent, "受控端")):
            latest = await client.get(f"{host}/api/v1/public/agent/latest")
            check(f"{label}域名提供安装包信息", latest.status_code == 200, str(latest.status_code))
            script = await client.get(f"{host}/api/v1/public/install.ps1", params={"code": args.code})
            check(f"{label}域名提供安装脚本", script.status_code == 200, str(script.status_code))
            check(f"{label}域名的脚本带正确版本", version in script.text, version)
        bad_code = await client.get(f"{agent}/api/v1/public/install.ps1", params={"code": "definitely-wrong"})
        check("错误注册码被拒绝", bad_code.status_code == 403, str(bad_code.status_code))

        group("控制台")
        login = await client.post(f"{console}/api/v1/auth/login", json={"password": args.password})
        check("控制台登录", login.status_code == 200, login.text[:160])
        if login.status_code != 200:
            return 1
        token = login.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        session = await client.get(f"{console}/api/v1/auth/session")
        check("会话 Cookie 生效", session.json().get("authenticated") is True)
        devices = await client.get(f"{console}/api/v1/devices", headers=auth)
        check("设备列表可读", devices.status_code == 200, str(devices.status_code))
        settings = await client.get(f"{console}/api/v1/settings", headers=auth)
        check("设置可读", settings.status_code == 200)
        updates = await client.get(f"{console}/api/v1/updates", headers=auth)
        latest_version = updates.json().get("latest_agent_version")
        check("已登记受控端安装包", bool(latest_version), str(updates.json().get("packages"))[:120])

        group("安装包")
        latest_info = (await client.get(f"{agent}/api/v1/public/agent/latest")).json()
        if latest_info.get("available"):
            package = await client.get(f"{agent}{latest_info['url']}")
            check("安装包可下载", package.status_code == 200 and len(package.content) == latest_info["size_bytes"], f"{package.status_code} {len(package.content)}")
            digest = hashlib.sha256(package.content).hexdigest()
            check("安装包 sha256 与声明一致", digest == latest_info["sha256"], digest[:16])
        else:
            check("已有可用安装包", False, json.dumps(latest_info))

        group("受控端通道")
        enroll = await client.post(
            f"{agent}/api/v1/agent/enroll",
            json={
                "code": args.code,
                "uid": args.device_uid,
                "hostname": "deploy-check",
                "os_name": "Windows",
                "os_version": "11",
                "arch": "AMD64",
                "agent_version": version,
                "sysinfo": {"cpu_percent": 1.0},
            },
        )
        check("受控端注册", enroll.status_code == 200, enroll.text[:160])
        if enroll.status_code != 200:
            return 1
        device_token = enroll.json()["token"]
        base_urls = enroll.json().get("base_urls") or []
        check("注册下发双域名", len(base_urls) >= 2, str(base_urls))

        ws_url = f"wss://{args.agent_host}/api/v1/agent/ws"
        try:
            async with websockets.connect(
                ws_url, additional_headers={"X-Device-Token": device_token}, ssl=True, open_timeout=20
            ) as socket:
                welcome = json.loads(await asyncio.wait_for(socket.recv(), 20))
                check("受控端 WebSocket 升级成功", welcome.get("t") == "welcome", str(welcome)[:120])
                await socket.send(json.dumps({"t": "heartbeat", "agent_version": version}))
        except Exception as exc:  # noqa: BLE001
            check("受控端 WebSocket 升级成功", False, repr(exc)[:200])

        device = (await client.get(f"{console}/api/v1/devices/{args.device_uid}", headers=auth)).json().get("device") or {}
        presence = device.get("presence") or {}
        check("设备出现在控制台", bool(device), "未找到")
        check("状态为 online（WS 已建立过）", presence.get("state") in {"online", "degraded"}, str(presence.get("state")))

        cleanup = await client.delete(f"{console}/api/v1/devices/{args.device_uid}", headers=auth)
        check("可以删除体检设备", cleanup.status_code in (200, 204), str(cleanup.status_code))

    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败 / {PASSED + len(FAILED)} 项：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print(f"全部通过：{PASSED} 项检查")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))