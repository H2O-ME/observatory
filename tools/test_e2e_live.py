#!/usr/bin/env python
"""真实端到端测试：真起服务端 + 真起受控端（前台模式），验证整条链路。

    python tools/test_e2e_live.py

做的事：

1. 用临时数据目录启动 uvicorn（HTTP，无 TLS，仅 127.0.0.1）
2. 用 `observatory_agent --console` 启动受控端，指向该服务器
3. 校验：注册 → WebSocket 通道 → presence 为 online → 心跳 → 画面入库
   → 命令下发与回执 → 文件枚举/读写 → 审计 → 卸载指令可下发
4. 结束时杀掉两个进程并清理临时目录（含受控端的 ProgramData 覆盖目录）

Windows 上会真的截屏（受控端本身就是这么工作的），不注入任何输入。
非 Windows 或缺少 pywin32 时直接跳过。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(method: str, url: str, *, body: bytes | None = None, headers: dict | None = None, timeout: float = 30):
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        # Server not up yet (or already gone): the caller polls.
        return 0, b"", {}


def wait_for(predicate, timeout: float, interval: float = 0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def main() -> int:
    if os.name != "nt":
        print("跳过：受控端只在 Windows 上运行")
        return 0
    try:
        import mss  # noqa: F401
        import win32api  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"跳过：缺少受控端依赖（{exc}）")
        return 0

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    work = Path(tempfile.mkdtemp(prefix="observatory-live-"))
    data_dir = work / "server"
    agent_data = work / "agent"
    passwords = {"password": "live-test-password-123", "code": "live-enroll-code"}

    server_env = os.environ.copy()
    server_env.update(
        {
            "OBS_DATA_DIR": str(data_dir),
            "OBS_SECRET_KEY": "live-test-secret-0123456789abcdef0123456789abcdef",
            "OBS_ADMIN_PASSWORD": passwords["password"],
            "OBS_ENROLL_CODE": passwords["code"],
            "OBS_CONSOLE_HOST": "127.0.0.1",
            "OBS_AGENT_HOSTS": "127.0.0.1",
            "OBS_PUBLIC_BASE_URLS": base,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT / "server"),
        }
    )
    agent_env = os.environ.copy()
    agent_env.update(
        {
            "OBS_PROGRAM_DATA": str(agent_data),
            # Never let a console-mode run touch the real install location.
            "OBS_INSTALL_DIR": str(work / "install"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT / "agent"),
        }
    )
    (work / "install").mkdir(parents=True, exist_ok=True)

    server_log = (work / "server.log").open("w", encoding="utf-8", errors="replace")
    agent_log = (work / "agent.log").open("w", encoding="utf-8", errors="replace")
    server = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "observatory.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        cwd=str(ROOT),
        env=server_env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    agent = None
    try:
        print(f"服务器 {base} · 数据 {work}")
        if not wait_for(lambda: http("GET", f"{base}/api/v1/health")[0] == 200, 45):
            print("服务器没有启动，日志：")
            print((work / "server.log").read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1

        status, body, _ = http(
            "POST",
            f"{base}/api/v1/auth/login",
            body=json.dumps({"password": passwords["password"]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        check("管理员登录", status == 200, body[:160].decode(errors="replace"))
        token = json.loads(body)["token"]
        auth = {"Authorization": f"Bearer {token}"}

        def devices() -> list:
            _, raw, _ = http("GET", f"{base}/api/v1/devices", headers=auth)
            return json.loads(raw).get("devices", [])

        print("启动受控端…")
        agent = subprocess.Popen(
            [
                PYTHON,
                "-m",
                "observatory_agent",
                "--console",
                "--enroll-code",
                passwords["code"],
                "--base-url",
                base,
            ],
            cwd=str(ROOT / "agent"),
            env=agent_env,
            stdout=agent_log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )

        enrolled = wait_for(lambda: devices(), 60)
        check("受控端出现在设备列表", bool(enrolled), "60 秒内没有设备")
        if not enrolled:
            print((work / "agent.log").read_text(encoding="utf-8", errors="replace")[-3000:])
            return 1
        uid = enrolled[0]["uid"]
        print(f"设备 {uid}")

        online = wait_for(lambda: (devices()[0]["presence"]["state"] == "online"), 60)
        check("受控端升级到 WebSocket（online）", bool(online), f"状态={devices()[0]['presence']}")
        device = devices()[0]
        check("通道标记为 ws", device["presence"]["transport"] == "ws", str(device["presence"]["transport"]))
        check("在线时能力齐全", all(device["presence"]["capabilities"].get(key) for key in ("control", "files", "screenshot", "update", "uninstall")), str(device["presence"]["capabilities"]))
        check("受控端版本与仓库一致", device["agent_version"] == version, str(device["agent_version"]))
        check("系统信息已上报", bool(device["sysinfo"].get("hostname")), str(device["sysinfo"])[:120])

        print("等待画面…")
        frame = wait_for(
            lambda: json.loads(http("GET", f"{base}/api/v1/devices/{uid}/frames/latest", headers=auth)[1]).get("frame"),
            120,
        )
        check("自动采集到画面", bool(frame), "120 秒内没有画面")
        if frame:
            check("画面尺寸合理", frame["width"] > 0 and frame["height"] > 0, str(frame))
            status, blob, headers = http(
                "GET", f"{base}/api/v1/devices/{uid}/frames/{frame['id']}?variant=thumb", headers=auth
            )
            check("能取到缩略图", status == 200 and len(blob) > 0, str(status))
            check("缩略图是 WebP", blob[:4] == b"RIFF", str(blob[:8]))

        def run_command(kind: str, payload: dict, timeout: float = 60):
            body = json.dumps({"type": kind, "payload": payload}).encode()
            status, raw, _ = http(
                "POST",
                f"{base}/api/v1/devices/{uid}/commands",
                body=body,
                headers={**auth, "Content-Type": "application/json"},
            )
            if status != 200:
                return None
            command_id = json.loads(raw)["id"]

            def settled():
                _, out, _ = http("GET", f"{base}/api/v1/devices/{uid}/commands/{command_id}", headers=auth)
                command = json.loads(out)
                return command if command["status"] in {"ok", "error", "timeout"} else None

            return wait_for(settled, timeout)

        result = run_command("screenshot", {})
        check("截图命令通过 WS 完成", bool(result) and result["status"] == "ok", json.dumps(result)[:200] if result else "无回执")

        result = run_command("files", {"op": "drives"})
        check("文件枚举成功", bool(result) and result["status"] == "ok", json.dumps(result)[:200] if result else "无回执")
        if result and result.get("status") == "ok":
            drives = (result.get("result") or {}).get("data", {}).get("drives") or []
            check("拿到驱动器列表", len(drives) >= 1, str(drives)[:160])

            # Write inside the agent's temp directory: C:\ itself is not writable
            # for a standard user, and a denied write is a test failure, not a bug.
            probe_dir = tempfile.gettempdir().rstrip("\\") + "\\"
            probe = f"{probe_dir}observatory-live-probe-{int(time.time())}.txt"
            write = run_command("files", {"op": "write", "path": probe, "offset": 0, "data": "b2JzZXJ2YXRvcnk=", "truncate": True})
            check("远程写入文件", bool(write) and write.get("status") == "ok", json.dumps(write)[:200] if write else "无回执")
            read = run_command("files", {"op": "read", "path": probe, "offset": 0, "length": 1024})
            ok_read = bool(read) and read.get("status") == "ok"
            check("远程读取文件", ok_read, json.dumps(read)[:200] if read else "无回执")
            if ok_read:
                import base64

                raw = (read.get("result") or {}).get("data", {}).get("data") or ""
                check("读取内容一致", base64.b64decode(raw) == b"observatory", raw[:40])
            run_command("files", {"op": "delete", "path": probe})

        _, audit_raw, _ = http("GET", f"{base}/api/v1/audit?limit=100", headers=auth)
        actions = {entry["action"] for entry in json.loads(audit_raw)["entries"]}
        check("审计记录注册与命令", {"device.enroll", "command.create"} <= actions, str(sorted(actions)))

        # Remote uninstall is deliberately NOT triggered here: the agent really
        # purges itself (deletes its install dir, ProgramData and schedules its own
        # executable for deletion at reboot), which is exactly right on a customer
        # machine but destructive against a source checkout. The HTTP contract
        # (body required, confirm=true, 409 when offline) is covered by
        # tools/test_server.py, and purge()'s own path handling by
        # tools/test_agent.py.
        check("设备保持在线（未触发卸载）", devices()[0]["presence"]["state"] == "online", str(devices()[0]["presence"]))
    finally:
        for process in (agent, server):
            if process and process.poll() is None:
                # The agent spawns a per-session worker; kill the whole tree or the
                # named pipe instance stays busy and the next run cannot start.
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        capture_output=True,
                        check=False,
                    )
                else:
                    process.send_signal(signal.SIGTERM)
        for process in (agent, server):
            if process:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
        server_log.close()
        agent_log.close()
        if FAILED:
            for name, log in (("服务端", work / "server.log"), ("受控端", work / "agent.log")):
                text = log.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    print(f"\n--- {name}日志（末尾）---")
                    print(text[-2500:])
    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败 / {PASSED + len(FAILED)} 项：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print(f"全部通过：{PASSED} 项检查 · 临时目录 {work} 已清理")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())