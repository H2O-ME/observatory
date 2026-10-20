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
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"VERSION", ".gitignore"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if LEGACY_NAME in text.lower():
            offenders.append(str(path.relative_to(ROOT)))
    check("仓库中不再出现废弃项目名", not offenders, str(offenders[:5]))


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

        device = client.get("/api/v1/devices/OBS-TEST0001", headers=headers).json()["device"]
        check("WS 连接后状态在线", device["presence"]["state"] == "online", device["presence"]["state"])
        check("在线状态可实时控制", device["presence"]["capabilities"]["control"] is True)

        reply = client.post(
            "/api/v1/devices/OBS-TEST0001/commands",
            headers=headers,
            json={"type": "sysinfo", "payload": {}},
        )
        check("在线设备可直接下发命令", reply.status_code == 200, reply.text[:120])
        pushed = agent_socket.receive_json()
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


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    print(f"Observatory {version} 自检 · 数据目录 {DATA_DIR}")

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
        websockets_flow(client, headers, device_token, version)
        audit_flow(client, headers)

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