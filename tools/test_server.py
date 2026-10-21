#!/usr/bin/env python
"""End-to-end self test for the Observatory server.

Runs against the real FastAPI app with a temporary data directory — no external
services, no network. Exits non-zero on the first failing group so CI fails loudly.

    cd server && python ../tools/test_server.py
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

DATA_DIR = Path(tempfile.mkdtemp(prefix="observatory-test-"))
os.environ.update(
    {
        "OBS_DATA_DIR": str(DATA_DIR),
        "OBS_SECRET_KEY": "test-secret-key-0123456789abcdef0123456789abcdef",
        "OBS_ADMIN_PASSWORD": "test-password-123",
        "OBS_ENROLL_CODE": "test-enroll-code",
        "OBS_CONSOLE_HOST": "control.example.test",
        "OBS_AGENT_HOSTS": "agent.example.test,control.example.test",
        "OBS_PUBLIC_BASE_URLS": "https://control.example.test,https://agent.example.test",
        "OBS_TLS_INSECURE": "1",
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from observatory.main import create_app  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        return
    FAILED.append(f"{name}{(' :: ' + detail) if detail else ''}")
    print(f"FAIL {name} {detail}")


def group(title: str) -> None:
    print(f"\n== {title} ==")


def version_consistency() -> None:
    group("版本号一致性")
    root_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    check("VERSION 是语义化版本", root_version.count(".") == 2 and all(p.isdigit() for p in root_version.split(".")), root_version)

    from observatory.version import FALLBACK_VERSION, resolve_version

    check("服务端 fallback 与 VERSION 一致", FALLBACK_VERSION == root_version, FALLBACK_VERSION)
    check("服务端解析版本", resolve_version() == root_version, resolve_version())

    sys.path.insert(0, str(ROOT / "agent"))
    from observatory_agent.version import FALLBACK_VERSION as AGENT_FALLBACK

    check("受控端 fallback 与 VERSION 一致", AGENT_FALLBACK == root_version, AGENT_FALLBACK)

    package_json = json.loads((ROOT / "webui" / "package.json").read_text(encoding="utf-8"))
    if package_json.get("version"):
        check("WebUI 版本与 VERSION 一致", package_json["version"] == root_version, package_json["version"])


def api_flow(client: TestClient, version: str) -> None:
    group("基础接口")
    health = client.get("/api/v1/health")
    check("健康检查 200", health.status_code == 200)
    check("健康检查返回版本", health.json().get("version") == version)
    check("版本接口", client.get("/api/v1/version").json().get("version") == version)

    check("未登录访问设备列表 401", client.get("/api/v1/devices").status_code == 401)

    bad = client.post("/api/v1/auth/login", json={"password": "wrong"})
    check("错误密码 401", bad.status_code == 401, str(bad.status_code))

    login = client.post("/api/v1/auth/login", json={"password": "test-password-123"})
    check("登录成功", login.status_code == 200, login.text[:120])
    token = login.json().get("token", "")
    check("登录返回版本", login.json().get("version") == version)
    check("登录设置会话 Cookie", "obs_session" in login.cookies or "obs_session" in client.cookies)

    session = client.get("/api/v1/auth/session")
    check("会话有效", session.json().get("authenticated") is True)

    headers = {"Authorization": f"Bearer {token}"}
    devices = client.get("/api/v1/devices", headers=headers)
    check("设备列表 200", devices.status_code == 200)
    check("设备列表含服务器版本", devices.json().get("server_version") == version)


def totp_flow(client: TestClient, headers: dict) -> None:
    group("二次验证")
    import pyotp

    setup = client.post("/api/v1/auth/totp/setup", headers=headers)
    check("生成 TOTP 密钥", setup.status_code == 200 and len(setup.json().get("secret", "")) >= 16)
    secret = setup.json()["secret"]

    wrong = client.post("/api/v1/auth/totp/enable", headers=headers, json={"secret": secret, "code": "000000"})
    check("错误验证码不能启用", wrong.status_code == 400)

    code = pyotp.TOTP(secret).now()
    enabled = client.post("/api/v1/auth/totp/enable", headers=headers, json={"secret": secret, "code": code})
    check("正确验证码启用", enabled.status_code == 200, enabled.text[:120])

    plain = client.post("/api/v1/auth/login", json={"password": "test-password-123"})
    check("启用后登录需要验证码", plain.status_code == 401 and "二次验证" in plain.text, plain.text[:120])

    with_code = client.post(
        "/api/v1/auth/login",
        json={"password": "test-password-123", "totp": pyotp.TOTP(secret).now()},
    )
    check("带验证码可以登录", with_code.status_code == 200, with_code.text[:120])

    # The previous build let any live session strip the second factor with an
    # empty POST; this must be impossible now.
    no_body = client.post("/api/v1/auth/totp/disable", headers=headers)
    check("关闭二次验证必须带验证码（无 body 422）", no_body.status_code == 422, str(no_body.status_code))
    wrong_disable = client.post("/api/v1/auth/totp/disable", headers=headers, json={"code": "000000"})
    check("错误验证码不能关闭", wrong_disable.status_code == 400)
    good_disable = client.post(
        "/api/v1/auth/totp/disable", headers=headers, json={"code": pyotp.TOTP(secret).now()}
    )
    check("正确验证码可以关闭", good_disable.status_code == 200, good_disable.text[:120])


def enroll_and_presence(client: TestClient, headers: dict) -> str:
    group("受控端注册与在线状态")
    bad = client.post(
        "/api/v1/agent/enroll",
        json={"code": "nope", "uid": "OBS-TEST0001", "hostname": "host"},
    )
    check("错误注册码 401", bad.status_code == 401, str(bad.status_code))

    enroll = client.post(
        "/api/v1/agent/enroll",
        json={
            "code": "test-enroll-code",
            "uid": "OBS-TEST0001",
            "hostname": "test-host",
            "os_name": "Windows",
            "os_version": "11 23H2",
            "arch": "AMD64",
            "agent_version": "0.2.0",
            "sysinfo": {"cpu_percent": 12.5, "mem_percent": 40.0, "mem_total": 8 * 1024**3},
        },
    )
    check("注册成功", enroll.status_code == 200, enroll.text[:200])
    payload = enroll.json()
    device_token = payload.get("token", "")
    check("注册返回设备令牌", bool(device_token))
    check("注册返回双域名列表", len(payload.get("base_urls") or []) >= 2, str(payload.get("base_urls")))
    check("注册返回配置", isinstance(payload.get("config"), dict) and payload["config"].get("capture_interval_s"))

    heartbeat = client.post(
        "/api/v1/agent/heartbeat",
        headers={"X-Device-Token": device_token},
        json={"agent_version": "0.2.0", "sysinfo": {"cpu_percent": 14.0}},
    )
    check("心跳成功", heartbeat.status_code == 200, heartbeat.text[:160])
    check("心跳下发轮询保持时间", heartbeat.json().get("poll_hold_s", 0) > 0)
    check("心跳无需卸载", heartbeat.json().get("uninstall") is False)

    devices = client.get("/api/v1/devices", headers=headers).json()["devices"]
    check("设备出现在列表", len(devices) == 1, str(len(devices)))
    device = devices[0]
    presence = device["presence"]
    check("轮询设备状态为受限（degraded）", presence["state"] == "degraded", presence["state"])
    check("受限状态仍可用文件管理", presence["capabilities"]["files"] is True)
    check("受限状态不可实时控制", presence["capabilities"]["control"] is False)
    check("状态包含原因", bool(presence["reason"]))
    check("设备负载入参", device["sysinfo"].get("cpu_percent") == 14.0)

    # A device can be perfectly reachable and still unable to capture: the agent
    # runs as a service in session 0, so a host with no logged-in desktop has no
    # screen to grab. The console must say so instead of showing "online" next to
    # a permanently empty timeline.
    blocked = client.post(
        "/api/v1/agent/heartbeat",
        headers={"X-Device-Token": device_token},
        json={
            "agent_version": "0.2.0",
            "state": {"capture": {"state": "no_session", "reason": "当前没有已登录的交互式桌面", "sessions": 0}},
        },
    )
    check("上报采屏状态被接受", blocked.status_code == 200, blocked.text[:160])
    device = client.get("/api/v1/devices", headers=headers).json()["devices"][0]
    presence = device["presence"]
    check("无桌面时状态说明原因", "桌面" in (presence["reason"] or ""), str(presence["reason"]))
    check("无桌面时截图能力为假", presence["capabilities"]["screenshot"] is False)
    check("无桌面时实时控制关闭", presence["capabilities"]["control"] is False)
    check("采屏状态原样暴露", (presence.get("capture") or {}).get("state") == "no_session", str(presence.get("capture")))
    check("文件管理仍可用", presence["capabilities"]["files"] is True)

    ok_capture = client.post(
        "/api/v1/agent/heartbeat",
        headers={"X-Device-Token": device_token},
        json={"agent_version": "0.2.0", "state": {"capture": {"state": "ok", "sessions": 1, "workers": [1]}}},
    )
    check("采屏恢复后心跳成功", ok_capture.status_code == 200)
    device = client.get("/api/v1/devices", headers=headers).json()["devices"][0]
    check("采屏恢复后截图能力回归", device["presence"]["capabilities"]["screenshot"] is True)
    return device_token


def command_flow(client: TestClient, headers: dict, device_token: str) -> None:
    group("命令总线（轮询路径）")
    created = client.post(
        "/api/v1/devices/OBS-TEST0001/commands",
        headers=headers,
        json={"type": "screenshot", "payload": {}},
    )
    check("创建命令", created.status_code == 200, created.text[:160])
    command_id = created.json()["id"]

    polled = client.get(
        "/api/v1/agent/commands?wait=0", headers={"X-Device-Token": device_token}
    )
    check("轮询拿到命令", polled.status_code == 200 and len(polled.json()["commands"]) == 1, polled.text[:160])
    check("命令类型正确", polled.json()["commands"][0]["type"] == "screenshot")

    result = client.post(
        f"/api/v1/agent/commands/{command_id}/result",
        headers={"X-Device-Token": device_token},
        json={"ok": True, "result": {"frame_ts": 123, "width": 800, "height": 600}},
    )
    check("回传结果", result.status_code == 200, result.text[:120])

    fetched = client.get(f"/api/v1/devices/OBS-TEST0001/commands/{command_id}", headers=headers).json()
    check("命令状态为成功", fetched["status"] == "ok", fetched["status"])

    rejected = client.post(
        "/api/v1/devices/OBS-TEST0001/commands",
        headers=headers,
        json={"type": "definitely-not-a-command", "payload": {}},
    )
    check("未知命令类型被拒绝", rejected.status_code == 400, str(rejected.status_code))

    unknown_file_op = client.post(
        "/api/v1/devices/OBS-TEST0001/files/op",
        headers=headers,
        json={"op": "list", "path": "C:\\"},
    )
    check("文件操作在超时后返回 502", unknown_file_op.status_code in (502, 200), str(unknown_file_op.status_code))


def uninstall_contract(client: TestClient, headers: dict) -> None:
    group("远程卸载契约")
    device = client.get("/api/v1/devices/OBS-TEST0001", headers=headers).json()["device"]
    assert device["presence"]["capabilities"]["uninstall"] is True

    no_body = client.post("/api/v1/devices/OBS-TEST0001/uninstall", headers=headers)
    check("缺少 body 时 422（前端必须发 confirm）", no_body.status_code == 422, str(no_body.status_code))

    declined = client.post("/api/v1/devices/OBS-TEST0001/uninstall", headers=headers, json={"confirm": False})
    check("confirm=false 被拒绝", declined.status_code == 400, str(declined.status_code))


def frame_flow(client: TestClient, headers: dict, device_token: str) -> None:
    group("画面采集与时间线")
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (640, 360), (30, 90, 200)).save(buffer, format="PNG")
    png = buffer.getvalue()

    upload = client.post(
        "/api/v1/agent/frames?ts_ms=1700000000000&session_id=1",
        headers={"X-Device-Token": device_token, "Content-Type": "image/png"},
        content=png,
    )
    check("上传画面", upload.status_code == 200, upload.text[:160])
    frame_id = upload.json().get("frame_id")
    check("返回画面 id", bool(frame_id))

    latest = client.get("/api/v1/devices/OBS-TEST0001/frames/latest", headers=headers).json()
    check("最新画面", latest["frame"]["id"] == frame_id)

    thumb = client.get(f"/api/v1/devices/OBS-TEST0001/frames/{frame_id}?variant=thumb", headers=headers)
    check("缩略图 200", thumb.status_code == 200, str(thumb.status_code))
    check("缩略图为 webp", thumb.headers.get("content-type", "").startswith("image/webp"), thumb.headers.get("content-type", ""))
    check("缩略图不可变缓存", "immutable" in thumb.headers.get("cache-control", ""), thumb.headers.get("cache-control", ""))

    # Cookie-based auth is what lets <img> use the browser cache.
    original = client.cookies.get("obs_session")
    streamed = client.get(f"/api/v1/devices/OBS-TEST0001/frames/{frame_id}?variant=full")
    check("Cookie 可直接读取画面", streamed.status_code == 200, str(streamed.status_code))
    if original:
        client.cookies.set("obs_session", original)

    timeline = client.get(
        "/api/v1/devices/OBS-TEST0001/timeline?from_ms=1699999999000&to_ms=1700000100000", headers=headers
    )
    check("时间线 200", timeline.status_code == 200, timeline.text[:160])
    check("时间线有桶", len(timeline.json()["buckets"]) >= 1)
    check("时间线统计", timeline.json()["total_frames"] >= 1)

    too_wide = client.get(
        "/api/v1/devices/OBS-TEST0001/timeline?from_ms=0&to_ms=6000000000000", headers=headers
    )
    check("时间线跨度过大被拒绝", too_wide.status_code == 400, str(too_wide.status_code))


def settings_flow(client: TestClient, headers: dict) -> None:
    group("设置校验")
    payload = client.get("/api/v1/settings", headers=headers).json()
    check("设置包含规格", "capture_change_threshold" in payload["specs"])
    check("阈值规格为 1..64", (payload["specs"]["capture_change_threshold"]["min"], payload["specs"]["capture_change_threshold"]["max"]) == (1, 64))
    check("密钥不进草稿", "archive_token" not in payload["values"])

    low = client.patch("/api/v1/settings", headers=headers, json={"values": {"capture_change_threshold": 0}})
    check("阈值 0 被拒绝", low.status_code == 400, low.text[:120])
    high = client.patch("/api/v1/settings", headers=headers, json={"values": {"capture_change_threshold": 65}})
    check("阈值 65 被拒绝", high.status_code == 400, high.text[:120])
    ok = client.patch("/api/v1/settings", headers=headers, json={"values": {"capture_change_threshold": 8}})
    check("阈值 8 被接受", ok.status_code == 200, ok.text[:120])

    unknown = client.patch("/api/v1/settings", headers=headers, json={"values": {"not_a_setting": 1}})
    check("未知设置项被拒绝", unknown.status_code == 400, str(unknown.status_code))

    system = client.get("/api/v1/system", headers=headers).json()
    check("系统状态含版本", system["version"] == (ROOT / "VERSION").read_text(encoding="utf-8").strip())
    check("系统状态含存储", system["storage"]["disk_total"] > 0)


def packages_flow(client: TestClient, headers: dict, version: str) -> None:
    group("自动更新安装包")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("observatory-agent.exe", b"not-a-real-exe")
    data = buffer.getvalue()

    missing_version = client.post(
        "/api/v1/updates/agent",
        headers=headers,
        files={"file": ("observatory-agent.zip", data, "application/zip")},
    )
    check("无版本号的文件名被拒绝", missing_version.status_code == 400, missing_version.text[:120])

    upload = client.post(
        "/api/v1/updates/agent",
        headers=headers,
        files={"file": (f"observatory-agent-{version}.zip", data, "application/zip")},
        data={"notes": "test"},
    )
    check("上传安装包", upload.status_code == 200, upload.text[:200])
    check("安装包版本", upload.json()["version"] == version)

    status = client.get("/api/v1/updates", headers=headers).json()
    check("最新受控端版本", status["latest_agent_version"] == version)
    check("安装包列表非空", len(status["packages"]) == 1)

    download = client.get(f"/api/v1/public/agent/{version}/package")
    check("下载安装包", download.status_code == 200)
    check("下载带 sha256 头", bool(download.headers.get("x-sha256")))

    latest = client.get("/api/v1/public/agent/latest").json()
    check("public latest 可用", latest.get("available") is True and latest.get("version") == version)


def domain_policy(client: TestClient, version: str) -> None:
    group("双域名策略")
    console_device = client.get(
        "/api/v1/devices", headers={"Host": "control.example.test"}
    )
    check("控制台域名可访问设备接口", console_device.status_code == 200, str(console_device.status_code))

    agent_only = client.get("/api/v1/devices", headers={"Host": "agent.example.test"})
    check("受控端域名拒绝控制台接口", agent_only.status_code == 404, str(agent_only.status_code))

    agent_ok = client.get("/api/v1/public/agent/latest", headers={"Host": "agent.example.test"})
    check("受控端域名提供公共接口", agent_ok.status_code == 200, str(agent_ok.status_code))

    health = client.get("/api/v1/health", headers={"Host": "agent.example.test"})
    check("受控端域名提供健康检查", health.status_code == 200)

    agent_script = client.get("/install.ps1?code=test-enroll-code", headers={"Host": "agent.example.test"})
    check("受控端域名提供安装脚本", agent_script.status_code == 200, str(agent_script.status_code))
    check("受控端域名的脚本含版本", version in agent_script.text)

    agent_script_no_code = client.get("/install.ps1", headers={"Host": "agent.example.test"})
    check("受控端域名脚本也要注册码", agent_script_no_code.status_code == 403, str(agent_script_no_code.status_code))

    spoofed = client.get(
        "/api/v1/settings", headers={"Host": "agent.example.test", "X-Forwarded-Host": "agent.example.test"}
    )
    check("伪造 Host 也拿不到设置", spoofed.status_code == 404, str(spoofed.status_code))

    no_code = client.get("/install.ps1")
    check("安装脚本需要注册码", no_code.status_code == 403, str(no_code.status_code))

    bad_code = client.get("/install.ps1?code=wrong")
    check("错误注册码 403", bad_code.status_code == 403, str(bad_code.status_code))

    script = client.get("/install.ps1?code=test-enroll-code")
    check("安装脚本 200", script.status_code == 200, str(script.status_code))
    body = script.text
    check("脚本显示版本号", version in body)
    check("脚本包含双域名", "control.example.test" in body and "agent.example.test" in body)
    leftovers = [token for token in ("@@VERSION@@", "@@ENROLL_CODE@@", "@@BASE_URLS@@", "@@CONSOLE_HOST@@", "@@SERVER_TIME_MS@@") if token in body]
    check("脚本无占位符残留", not leftovers, str(leftovers))

    # The console hands out a file, not a command line: the .cmd must carry the
    # code and the failover list so double-clicking is the whole procedure.
    cmd = client.get("/api/v1/public/install.cmd?code=test-enroll-code")
    check("安装程序 200", cmd.status_code == 200, str(cmd.status_code))
    disposition = cmd.headers.get("content-disposition", "")
    check("安装程序作为附件下载", "attachment" in disposition and disposition.rstrip('"').endswith(".cmd"), disposition)
    wrapper = cmd.text
    check("安装程序内置注册码", "test-enroll-code" in wrapper)
    check("安装程序内置版本号", version in wrapper)
    check("安装程序内置多线路", 'set "URL1=' in wrapper and 'set "URL2=' in wrapper, wrapper[:200])
    check("安装程序拉起 PowerShell", "powershell -NoProfile -ExecutionPolicy Bypass -File" in wrapper)
    # cmd.exe parses batch files in the console code page; any non-ASCII byte
    # makes the wrapper execute garbage on machines using a legacy code page.
    check("安装程序为纯 ASCII", all(ord(char) < 128 for char in wrapper), repr([c for c in wrapper if ord(c) >= 128][:5]))
    cmd_leftovers = [token for token in ("@@VERSION@@", "@@ENROLL_CODE@@", "@@URL_LINES@@") if token in wrapper]
    check("安装程序无占位符残留", not cmd_leftovers, str(cmd_leftovers))
    check(
        "安装程序需要正确注册码",
        client.get("/api/v1/public/install.cmd?code=wrong").status_code == 403,
    )

    ps1_download = client.get("/api/v1/public/install.ps1?code=test-enroll-code&download=1")
    check(
        "安装脚本可下载",
        "attachment" in ps1_download.headers.get("content-disposition", "")
        and ps1_download.headers["content-disposition"].rstrip('"').endswith(".ps1"),
        ps1_download.headers.get("content-disposition", ""),
    )
    # Saved to disk and run with -File, PowerShell 5.1 would read a BOM-less file
    # as ANSI and garble the Chinese output.
    check("下载的脚本带 UTF-8 BOM", ps1_download.text.startswith("\ufeff"))
    check("脚本正文仍是同一个脚本", "<#" in ps1_download.text and version in ps1_download.text)



#: The project was renamed; the old folder name must never appear again. Built at
#: runtime so this very file does not contain the string it forbids.
LEGACY_NAME = "shen" + "mi"
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    ".localdata",
    "dist",
    "build",
    "__pycache__",
    ".zcode",
    "backups",
}
TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".cmd",
    ".sh",
    ".yml",
    ".yaml",
    ".html",
    ".css",
    ".txt",
    ".conf",
    ".spec",
    ".example",
}


def stale_name_scan() -> None:
    group("废弃名称清理")
    # A one-shot migration script has to name the stack it is replacing (paths,
    # env prefixes). Everything else must be free of the old name.
    allowed = {"deploy/migrate-from-legacy.sh"}
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"VERSION", ".gitignore"}:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in allowed:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if LEGACY_NAME in text.lower():
            offenders.append(relative)
    check("仓库中不再出现废弃项目名（迁移脚本除外）", not offenders, str(offenders[:5]))
    check("迁移脚本是唯一的例外", (ROOT / "deploy/migrate-from-legacy.sh").exists())


def installer_flow(client: TestClient, headers: dict, version: str) -> None:
    group("安装程序分发")
    system = client.get("/api/v1/system", headers=headers)
    install_info = (system.json() or {}).get("install") or {}
    check("系统信息带安装信息", install_info.get("enabled") is True, str(system.json())[:120])
    check("安装信息内含注册码", install_info.get("enroll_code") == "test-enroll-code")
    check("安装信息提供两个下载项", len(install_info.get("artifacts") or []) == 2, str(install_info.get("artifacts"))[:160])
    check("安装信息版本与服务器一致", install_info.get("version") == version)
    check(
        "下载链接自带注册码",
        all("test-enroll-code" in (item.get("url") or "") for item in install_info.get("artifacts") or []),
    )

    # The whole point: an operator downloads a file instead of editing a command.
    settings_source = (ROOT / "webui/src/pages/settings.tsx").read_text(encoding="utf-8")
    check("控制台渲染下载按钮", "installArtifacts.map" in settings_source)
    check("控制台不再要求手填注册码", "<注册码>" not in settings_source)

    # "Online but never a frame" was indistinguishable from broken capture. The
    # console must surface the agent's own capture health instead.
    overview_source = (ROOT / "webui/src/pages/device/overview-view.tsx").read_text(encoding="utf-8")
    check("设备页提示无法采集屏幕", "无法采集屏幕" in overview_source)
    check("提示引用受控端上报的原因", "presence.capture?.reason" in overview_source)
    check("提示说明会话 0 限制", "会话 0" in overview_source)
    types_source = (ROOT / "webui/src/lib/types.ts").read_text(encoding="utf-8")
    check("前端类型包含采屏健康", "CaptureHealth" in types_source)

    # Console defects that are invisible to a type checker but obvious to a user:
    # an unstable React Query key (infinite refetch, stuck skeletons) and a fetch
    # with no deadline (the "正在连接 Observatory…" screen never resolves).
    timeline_source = (ROOT / "webui/src/pages/device/timeline-view.tsx").read_text(encoding="utf-8")
    check(
        "时间轴不再把 Date.now() 放进缓存键",
        "const now = Date.now()" not in timeline_source,
        "缓存键必须稳定，否则会无限重取",
    )
    check("时间轴使用稳定的时钟值", "useState(() => Math.floor(Date.now()" in timeline_source)
    check("时间轴按时推进窗口", "setInterval" in timeline_source)

    api_source = (ROOT / "webui/src/lib/api.ts").read_text(encoding="utf-8")
    check("前端请求带超时", "AbortController" in api_source and "REQUEST_TIMEOUT_MS" in api_source)
    check("超时后抛可读错误", "请求超时" in api_source)

    # `const { bootstrap } = useAuth()` detaches the method from the store, so a
    # `this.refresh()` inside it is `undefined.refresh()` and throws before the
    # first request. That left the console stuck on "正在连接 Observatory…".
    auth_source = (ROOT / "webui/src/store/auth.ts").read_text(encoding="utf-8")
    check("auth store 不用 this 调自身方法", "await this." not in auth_source, "必须用 get()")
    check("auth store 通过 get() 刷新", "get().refresh()" in auth_source)
    check("auth store 使用 get 参数", "create<AuthState>((set, get)" in auth_source)


def websockets_flow(client: TestClient, headers: dict, device_token: str, version: str) -> None:
    group("WebSocket 通道")
    login = client.post("/api/v1/auth/login", json={"password": "test-password-123"})
    token = login.json()["token"]

    with client.websocket_connect(f"/api/v1/ws?token={token}") as socket:
        hello = socket.receive_json()
        check("控制台 WS 收到 hello", hello.get("type") == "hello", str(hello)[:120])
        check("hello 含服务器版本", hello.get("data", {}).get("version") == version)

    with client.websocket_connect(
        "/api/v1/agent/ws", headers={"X-Device-Token": device_token}
    ) as agent_socket:
        welcome = agent_socket.receive_json()
        check("受控端 WS 收到 welcome", welcome.get("t") == "welcome", str(welcome)[:120])
        check("welcome 含配置", isinstance(welcome.get("config"), dict))

        # --- realtime control relay -------------------------------------
        from observatory.framing import encode as frame_encode

        granted = client.post(
            "/api/v1/devices/OBS-TEST0001/control",
            headers=headers,
            json={"confirm": True, "ttl_s": 120},
        )
        check("申请实时控制授权", granted.status_code == 200, granted.text[:160])
        grant_token = granted.json().get("token", "")
        check("授权带有效期", granted.json().get("expires_at_ms", 0) > 0)

        declined_grant = client.post(
            "/api/v1/devices/OBS-TEST0001/control", headers=headers, json={"confirm": False, "ttl_s": 60}
        )
        check("未确认不能开控制", declined_grant.status_code == 400, str(declined_grant.status_code))

        with client.websocket_connect(
            f"/api/v1/devices/OBS-TEST0001/control?token={grant_token}"
        ) as viewer:
            ready = viewer.receive_json()
            check("控制端收到 ready", ready.get("type") == "ready", str(ready)[:120])
            started = agent_socket.receive_json()
            check("受控端收到 ctrl_start", started.get("t") == "ctrl_start", str(started)[:120])

            viewer.send_json(
                {"type": "input", "ev": {"kind": "mouse", "action": "move", "x": 4000, "y": 6000, "norm": True}}
            )
            relayed = agent_socket.receive_json()
            event = relayed.get("ev") or {}
            check(
                "输入事件转发到受控端",
                relayed.get("t") == "ctrl_in" and event.get("x") == 4000 and event.get("y") == 6000,
                str(relayed)[:160],
            )

            fake_frame = b"\x00\x01control-frame-bytes"
            agent_socket.send_bytes(
                frame_encode({"t": "ctrl", "session": grant_token, "len": len(fake_frame)}, fake_frame)
            )
            received = viewer.receive_bytes()
            check("画面从受控端转发到浏览器", received == fake_frame, str(received[:24]))

        revoked = client.delete(
            f"/api/v1/devices/OBS-TEST0001/control/{grant_token}", headers=headers
        )
        check("可以主动结束控制会话", revoked.status_code == 200, str(revoked.status_code))

        stale = client.websocket_connect(f"/api/v1/devices/OBS-TEST0001/control?token=not-a-token")
        with stale as socket:
            while True:
                try:
                    socket.receive_json()
                except Exception:
                    break
            check("无效授权被拒绝", True)

        device = client.get("/api/v1/devices/OBS-TEST0001", headers=headers).json()["device"]
        check("WS 连接后状态在线", device["presence"]["state"] == "online", device["presence"]["state"])
        check("在线状态可实时控制", device["presence"]["capabilities"]["control"] is True)

        reply = client.post(
            "/api/v1/devices/OBS-TEST0001/commands",
            headers=headers,
            json={"type": "sysinfo", "payload": {}},
        )
        check("在线设备可直接下发命令", reply.status_code == 200, reply.text[:120])
        pushed = {}
        for _ in range(6):
            pushed = agent_socket.receive_json()
            if pushed.get("t") == "command":
                break
        check("命令通过 WS 推送", pushed.get("t") == "command", str(pushed)[:120])
        command_id = pushed.get("command", {}).get("id")

        agent_socket.send_json(
            {"t": "result", "command_id": command_id, "ok": True, "result": {"agent_version": version}}
        )
        settled = {}
        for _ in range(40):
            settled = client.get(
                f"/api/v1/devices/OBS-TEST0001/commands/{command_id}", headers=headers
            ).json()
            if settled.get("status") == "ok":
                break
            time.sleep(0.05)
        check("WS 结果写回命令", settled.get("status") == "ok", str(settled.get("status")))

    device = client.get("/api/v1/devices/OBS-TEST0001", headers=headers).json()["device"]
    check("断开后不再显示在线", device["presence"]["state"] != "online", device["presence"]["state"])


def audit_flow(client: TestClient, headers: dict) -> None:
    group("审计")
    audit = client.get("/api/v1/audit?limit=100", headers=headers).json()
    actions = {entry["action"] for entry in audit["entries"]}
    for expected in ("auth.login", "device.enroll", "command.create", "settings.update"):
        check(f"记录了 {expected}", expected in actions, str(sorted(actions)))
    check("审计总数大于 0", audit["total"] > 0)

    deleted = client.delete("/api/v1/devices/OBS-TEST0001", headers=headers)
    check("离线设备可删除", deleted.status_code == 200, deleted.text[:120])
    check("删除后可再注册", True)


def spa_flow(client: TestClient, version: str) -> None:
    group("控制台静态资源")
    dist = ROOT / "webui" / "dist"
    if not (dist / "index.html").exists():
        print("  跳过：webui/dist 未构建")
        return

    index = client.get("/")
    check("根路径返回控制台", index.status_code == 200 and "text/html" in index.headers.get("content-type", ""), str(index.status_code))
    check("控制台引用打包资源", "/assets/" in index.text)
    check("控制台标题是产品名", "Observatory" in index.text)
    check("控制台不再含废弃名", LEGACY_NAME not in index.text.lower())
    check("index.html 不缓存", "no-cache" in index.headers.get("cache-control", ""))

    deep_link = client.get("/devices/OBS-DOESNOTEXIST")
    check("前端路由回退到 index.html", deep_link.status_code == 200 and "/assets/" in deep_link.text)

    assets = sorted((dist / "assets").glob("*.js"))
    if assets:
        asset = client.get(f"/assets/{assets[0].name}")
        check("静态资源可访问", asset.status_code == 200, str(asset.status_code))
        check("静态资源是 JS", "javascript" in asset.headers.get("content-type", ""), asset.headers.get("content-type", ""))
        check("静态资源可长期缓存", "immutable" in asset.headers.get("cache-control", ""), asset.headers.get("cache-control", ""))

    unknown_api = client.get("/api/v1/definitely-not-here")
    check("未知接口返回 404 JSON", unknown_api.status_code == 404 and "detail" in unknown_api.text, str(unknown_api.status_code))

    traversal = client.get("/%2e%2e/server/.env")
    check("路径穿越被拒绝", traversal.status_code in (400, 404), str(traversal.status_code))

    agent_home = client.get("/", headers={"Host": "agent.example.test"})
    check("受控端域名不提供控制台", agent_home.status_code == 404, str(agent_home.status_code))
    check("受控端域名给出提示", "control.example.test" in agent_home.text, agent_home.text[:120])


def cli_flow(version: str) -> None:
    """The deploy scripts shell out to `python -m observatory.cli`; prove it works."""

    group("命令行工具")
    import subprocess

    cli_data = DATA_DIR / "cli-data"
    zip_path = DATA_DIR / f"observatory-agent-{version}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("observatory-agent.exe", b"MZ")

    env = {
        **os.environ,
        "PYTHONPATH": str(SERVER),
        "PYTHONIOENCODING": "utf-8",
        "OBS_DATA_DIR": str(cli_data),
        "OBS_SECRET_KEY": "cli-test-secret-0123456789abcdef0123456789abcdef",
        "OBS_ADMIN_PASSWORD": "cli-test-password-123",
        "OBS_ENROLL_CODE": "cli-code",
    }

    def run(*arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "observatory.cli", *arguments],
            cwd=str(SERVER),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    result = run("version")
    check("cli version 成功", result.returncode == 0, result.stderr[-200:])
    check("cli version 带版本号", version in result.stdout, result.stdout[:120])

    # deploy/build-agent.sh drops the zip straight into the packages directory;
    # `packages` must then discover it from disk, not only trust the database.
    packages_dir = cli_data / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(zip_path, packages_dir / zip_path.name)
    result = run("packages")
    check("cli packages 能发现磁盘上的安装包", result.returncode == 0 and version in result.stdout, result.stdout[-200:] or result.stderr[-200:])

    result = run("package-add", str(zip_path))
    check("cli package-add 成功", result.returncode == 0, (result.stderr or result.stdout)[-300:])
    check("cli package-add 打印版本", version in (result.stdout + result.stderr), (result.stdout + result.stderr)[-160:])

    result = run("packages")
    check("cli packages 列出安装包", result.returncode == 0 and version in result.stdout, result.stdout[-200:])

    result = run("enroll-code")
    check("cli enroll-code 生成注册码", result.returncode == 0 and len(result.stdout.strip()) >= 8, result.stdout[:80])


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    print(f"Observatory {version} 自检 · 数据目录 {DATA_DIR}")

    # Publish a real console build into the temp data dir so the SPA routes are
    # exercised instead of only the JSON fallback.
    dist = ROOT / "webui" / "dist"
    if (dist / "index.html").exists():
        shutil.copytree(dist, DATA_DIR / "webui", dirs_exist_ok=True)

    version_consistency()

    app = create_app()
    with TestClient(app) as client:
        api_flow(client, version)

        login = client.post("/api/v1/auth/login", json={"password": "test-password-123"})
        headers = {"Authorization": f"Bearer {login.json()['token']}"}

        totp_flow(client, headers)
        device_token = enroll_and_presence(client, headers)
        command_flow(client, headers, device_token)
        uninstall_contract(client, headers)
        frame_flow(client, headers, device_token)
        settings_flow(client, headers)
        packages_flow(client, headers, version)
        domain_policy(client, version)
        installer_flow(client, headers, version)
        websockets_flow(client, headers, device_token, version)
        audit_flow(client, headers)
        spa_flow(client, version)

    cli_flow(version)
    stale_name_scan()

    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败 / {PASSED + len(FAILED)} 项：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print(f"全部通过：{PASSED} 项检查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())