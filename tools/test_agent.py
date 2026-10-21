#!/usr/bin/env python
"""受控端自检。

纯逻辑部分在任何平台都能跑；涉及 Windows 的采集 / 输入 / 文件操作在有 pywin32 时
自动启用，缺依赖时跳过而不失败（Linux CI 也能跑）。

    python tools/test_agent.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

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


def version_and_config() -> None:
    group("版本与配置")
    from observatory_agent.version import FALLBACK_VERSION, is_newer, parse_version, resolve_version

    root_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    check("fallback 与 VERSION 一致", FALLBACK_VERSION == root_version, FALLBACK_VERSION)
    check("解析版本", resolve_version() == root_version, resolve_version())
    check("语义化比较", parse_version("0.2.10") > parse_version("0.2.9"))
    check("新版本判断", is_newer("0.2.1", "0.2.0"))
    check("同版本不算新", not is_newer("0.2.0", "0.2.0"))
    check("旧版本不算新", not is_newer("0.1.9", "0.2.0"))

    from observatory_agent.config import AgentConfig, PROGRAM_DATA, new_device_uid

    check("数据目录在 ProgramData 下", "Observatory" in str(PROGRAM_DATA), str(PROGRAM_DATA))
    uid = new_device_uid()
    check("设备 id 带前缀", uid.startswith("OBS-"), uid)
    check("设备 id 稳定", uid == new_device_uid())

    config = AgentConfig(device_uid=uid, token="t", base_urls=["https://a.example", "https://b.example"])
    interval = config.setting("capture_interval_s", 30)
    check("默认采集间隔合理", isinstance(interval, int) and 5 <= interval <= 3600, str(interval))
    heartbeat = config.setting("heartbeat_interval_s", 20)
    check("心跳间隔有默认值", isinstance(heartbeat, int) and heartbeat > 0, str(heartbeat))
    check("默认双域名", len(config.base_urls) >= 2, str(config.base_urls))

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        config.save(path)
        check("配置是合法 JSON", json.loads(path.read_text(encoding="utf-8"))["device_uid"] == uid)
        loaded = AgentConfig.load(path)
        check("配置可持久化", loaded.device_uid == uid)
        check("配置保留地址列表", loaded.base_urls == config.base_urls, str(loaded.base_urls))

        changed = loaded.apply_server_config({"capture_interval_s": 90, "jpeg_quality": 40})
        check("服务端配置生效", changed is True and loaded.setting("capture_interval_s") == 90)
        again = loaded.apply_server_config({"capture_interval_s": 90, "jpeg_quality": 40})
        check("相同配置不重复写盘", again is False)


def framing() -> None:
    group("帧协议")
    from observatory_agent.pipe_proto import decode, encode

    payload = b"hello-pipe"
    header, body = decode(encode({"kind": "frame", "len": len(payload)}, payload))
    check("头部长度前缀正确", header["kind"] == "frame" and header["len"] == len(payload))
    check("负载完整", body == payload)

    big = os.urandom(64 * 1024)
    header, body = decode(encode({"kind": "ctrl", "len": len(big)}, big))
    check("大负载往返", body == big and header["len"] == len(big))

    try:
        decode(b"\x00\x00")
        check("截断帧抛错", False, "没有抛异常")
    except Exception:
        check("截断帧抛错", True)


def transport() -> None:
    group("传输阶梯")
    from observatory_agent.transport import LEVEL_POLL_LONG, LEVEL_POLL_SHORT, LEVEL_WS, level_name

    levels = {LEVEL_WS, LEVEL_POLL_LONG, LEVEL_POLL_SHORT}
    check("三级阶梯都有定义", levels == {0, 1, 2}, str(levels))
    check("级别名称", level_name(LEVEL_WS) == "ws" and level_name(LEVEL_POLL_LONG) == "long-poll")

    from observatory_agent.client import BaseRouter

    router = BaseRouter(["https://a.example", "https://b.example", "https://c.example"])
    check("初始使用第一个地址", router.current == "https://a.example", router.current)
    router.rotate()
    check("切换后使用备用地址", router.current != "https://a.example", router.current)
    check("备用地址在候选内", router.current in router.urls, router.current)
    for _ in range(5):
        router.rotate()
    check("轮换不会越界", router.current in router.urls, router.current)


def windows_features() -> None:
    group("Windows 功能（不可用时跳过）")
    try:
        import win32api  # noqa: F401
    except Exception:
        print("  跳过：当前平台没有 pywin32")
        return

    from observatory_agent import capture, fs_ops, input as input_mod, sysinfo
    import base64

    listed = fs_ops.perform("drives", {})
    drives = (listed.get("data") or {}).get("drives") or []
    check("能枚举驱动器", listed.get("ok") is True and len(drives) >= 1, str(listed)[:160])
    check("驱动器有路径", all(entry.get("path") for entry in drives), str(drives)[:160])

    if drives:
        root = Path(drives[0]["path"])
        listing = fs_ops.perform("list", {"path": str(root)})
        check("能列根目录", listing.get("ok") is True, str(listing)[:160])
        check("根目录有条目", len((listing.get("data") or {}).get("entries") or []) >= 1)

        with tempfile.TemporaryDirectory(dir=root) as tmp:
            target = Path(tmp) / "probe.txt"
            payload = base64.b64encode(b"observatory").decode()
            written = fs_ops.perform(
                "write", {"path": str(target), "offset": 0, "data": payload, "truncate": True}
            )
            check("能写文件", written.get("ok") is True, str(written)[:160])
            read = fs_ops.perform("read", {"path": str(target), "offset": 0, "length": 1024})
            check("能读文件", read.get("ok") is True, str(read)[:160])
            raw = (read.get("data") or {}).get("data") or ""
            check("内容一致", base64.b64decode(raw) == b"observatory", str(raw)[:40])
            digest = fs_ops.perform("hash", {"path": str(target)})
            sha = ((digest.get("data") or {}).get("sha256") or "")
            check("能算哈希", digest.get("ok") is True and len(sha) == 64, str(digest)[:160])
            renamed = fs_ops.perform("rename", {"path": str(target), "target": "probe2.txt"})
            check("能改名", renamed.get("ok") is True, str(renamed)[:160])

    info = sysinfo.collect()
    check("采集到系统信息", "cpu_percent" in info and "mem_percent" in info, str(list(info))[:160])
    check("系统信息含主机名", bool(info.get("hostname")))

    screen_w, screen_h = input_mod._screen_size()
    check("能取屏幕尺寸", screen_w > 0 and screen_h > 0, f"{screen_w}x{screen_h}")

    point = input_mod._coordinates({"x": 10000, "y": 10000, "norm": True})
    check("归一化坐标落在屏幕内", 0 <= point[0] <= screen_w and 0 <= point[1] <= screen_h, str(point))

    data, width, height, score = capture.grab(max_width=640, quality=40)
    check("能采集画面", len(data) > 0)
    check("画面是 WebP", data[:4] == b"RIFF" and data[8:12] == b"WEBP", str(data[:12]))
    check("画面有尺寸", width > 0 and height > 0, f"{width}x{height}")
    check("宽度受限", width <= 640, str(width))
    check("变化分数在范围内", 0 <= score <= 255, str(score))


def update_pipeline() -> None:
    group("自动更新流程")
    import zipfile

    from observatory_agent import updater

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        archive = work / "observatory-agent-0.2.1.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("observatory-agent/observatory-agent.exe", b"MZ-new")
            bundle.writestr("observatory-agent/_internal/lib.dll", b"dll")
            bundle.writestr("observatory-agent/VERSION", b"0.2.1\n")

        extracted = updater.extract_payload(archive, work / "payload")
        check("解包后定位到 exe 所在目录", (extracted / "observatory-agent.exe").exists(), str(extracted))
        check("解包保留内部文件", (extracted / "_internal" / "lib.dll").exists())

        evil = work / "evil.zip"
        with zipfile.ZipFile(evil, "w") as bundle:
            bundle.writestr("../../escaped.txt", b"nope")
            bundle.writestr("observatory-agent.exe", b"MZ")
        try:
            updater.extract_payload(evil, work / "evil-payload")
            check("拒绝越权的安装包路径", False, "没有抛异常")
        except Exception:
            check("拒绝越权的安装包路径", not (work.parent / "escaped.txt").exists())

        # Swap: payload replaces target, old build is moved aside, service hooks
        # are stubbed so this never touches the real machine.
        payload = work / "new-build"
        payload.mkdir()
        (payload / "observatory-agent.exe").write_bytes(b"MZ-0.2.1")
        target = work / "install"
        target.mkdir()
        (target / "observatory-agent.exe").write_bytes(b"MZ-0.2.0")
        (target / "stale.txt").write_text("old", encoding="utf-8")

        calls = {"stopped": 0, "started": 0, "cleaned": 0}
        original = (
            updater._wait_for_process_exit,
            updater._stop_service,
            updater._start_service,
            updater._cleanup_old,
        )
        updater._wait_for_process_exit = lambda *a, **k: None
        updater._stop_service = lambda *a, **k: calls.__setitem__("stopped", calls["stopped"] + 1)
        updater._start_service = lambda *a, **k: calls.__setitem__("started", calls["started"] + 1)
        updater._cleanup_old = lambda *a, **k: calls.__setitem__("cleaned", calls["cleaned"] + 1)
        try:
            code = updater.apply_now(str(payload), str(target), 0, "0.2.1")
        finally:
            (
                updater._wait_for_process_exit,
                updater._stop_service,
                updater._start_service,
                updater._cleanup_old,
            ) = original

        check("更新助手返回成功", code == 0, str(code))
        check("新版本已就位", (target / "observatory-agent.exe").read_bytes() == b"MZ-0.2.1")
        check("旧版本文件不再存在", not (target / "stale.txt").exists())
        check("更新前先停服务", calls["stopped"] == 1, str(calls))
        check("更新后重启服务", calls["started"] == 1, str(calls))
        check("清理旧目录", calls["cleaned"] == 1, str(calls))

        missing = work / "not-there"
        check("载荷缺失时返回错误", updater.apply_now(str(missing), str(target), 0, "0.2.2") == 2)
        check("载荷缺失时不破坏现有安装", (target / "observatory-agent.exe").read_bytes() == b"MZ-0.2.1")


def uninstall_paths() -> None:
    group("卸载清理范围（只碰配置的目录）")
    try:
        import win32api  # noqa: F401
    except Exception:
        print("  跳过：当前平台没有 pywin32")
        return

    from observatory_agent import uninstall

    with tempfile.TemporaryDirectory() as tmp:
        install = Path(tmp) / "install"
        program_data = Path(tmp) / "programdata"
        (install / "sub").mkdir(parents=True)
        (install / "observatory-agent.exe").write_bytes(b"MZ")
        (install / "sub" / "lib.dll").write_bytes(b"x")
        program_data.mkdir(parents=True)
        (program_data / "config.json").write_text("{}", encoding="utf-8")
        keep = Path(tmp) / "keep.txt"
        keep.write_text("do not touch", encoding="utf-8")

        # Replace the two steps that reach outside the test sandbox.
        original_footprint = uninstall._remove_footprint
        original_self_delete = uninstall._schedule_self_delete
        original_program_data = uninstall.PROGRAM_DATA
        uninstall._remove_footprint = lambda: None
        uninstall._schedule_self_delete = lambda: None
        uninstall.PROGRAM_DATA = program_data
        try:
            uninstall.purge(0, str(install))
        finally:
            uninstall._remove_footprint = original_footprint
            uninstall._schedule_self_delete = original_self_delete
            uninstall.PROGRAM_DATA = original_program_data

        check("删除安装目录", not install.exists())
        check("删除数据目录", not program_data.exists())
        check("不碰无关文件", keep.exists())


def session_lifecycle() -> None:
    """Regressions for the bug that produced zero frames on every device.

    Two independent faults kept the capture worker from ever running inside the
    user's session: the session launcher referenced a constant that does not
    exist, and the session scanner retired sessions it never owned.
    """

    group("会话与采屏")
    from observatory_agent import session_launch

    check("MAXIMUM_ALLOWED 常量存在", session_launch.MAXIMUM_ALLOWED == 0x02000000)
    offenders = [
        path.name
        for path in (ROOT / "agent" / "observatory_agent").glob("*.py")
        if any(
            "win32security.MAXIMUM_ALLOWED" in line.split("#", 1)[0]
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        )
    ]
    check("不再引用 win32security 里不存在的常量", not offenders, str(offenders))

    from observatory_agent import runner as runner_module

    managed: set[int] = set()

    class FakePipe:
        def __init__(self) -> None:
            import threading

            self.connected = threading.Event()

    runner = runner_module.AgentRunner.__new__(runner_module.AgentRunner)

    class _Pipes(dict):
        pass

    runner._pipes = _Pipes()
    runner._processes = {}
    # A listener session (RDP-Tcp reports id 65537) must not be treated as a
    # managed session just because a worker hello mentioned it.
    runner._sessions = {65537: {"username": "", "connected": False}, 1: {"username": "u", "connected": True}}
    runner._pipes[1] = FakePipe()
    managed = {1, 65537}
    retired = [session_id for session_id in list(runner._sessions) if not runner._manages_session(session_id)]
    check("未托管的监听会话被忽略", retired == [65537], str(retired))
    check("托管会话仍被识别", runner._manages_session(1))

    health = runner_module.AgentRunner._capture_health(runner)
    check("拉起过程中上报启动中", health["state"] == "starting", str(health))
    runner._mark_capture_blocked(1, "无法进入用户会话，屏幕采集不可用")
    health = runner_module.AgentRunner._capture_health(runner)
    check("进入会话失败时上报阻塞", health["state"] == "blocked", str(health))
    check("阻塞原因可读", "屏幕采集" in health["reason"], health["reason"])
    runner._pipes[1].connected.set()
    health = runner_module.AgentRunner._capture_health(runner)
    check("有采屏进程时上报正常", health["state"] == "ok", str(health))


def large_result_transport() -> None:
    """A file read must not stall because its base64 exceeds the header limit."""

    group("大结果回传")
    import base64

    from observatory_agent.pipe_proto import MAX_HEADER_BYTES, FramingError, decode, encode
    from observatory_agent.worker import RESULT_INLINE_LIMIT

    blob = base64.b64encode(os.urandom(1024 * 1024)).decode("ascii")
    check("阈值小于头部上限", RESULT_INLINE_LIMIT < MAX_HEADER_BYTES, str(RESULT_INLINE_LIMIT))
    oversized = {"t": "result", "id": "x", "result": {"data": blob}}
    try:
        encode(oversized)
        raised = False
    except FramingError:
        raised = True
    check("大 base64 确实放不进头部", raised)

    raw = base64.b64decode(blob)
    header, payload = decode(
        encode({"t": "result", "id": "x", "result": {"data_encoding": "binary"}}, raw)
    )
    check("二进制载荷可承载 1MiB", payload == raw and header["result"]["data_encoding"] == "binary")


def helper_cli_contract() -> None:
    """The update helper must be startable with the exact arguments we spawn.

    This is the regression that mattered: ``spawn_apply_helper`` passed
    ``--version 0.2.1``, but ``--version`` was a bare flag and the version option
    was named differently, so the helper died on an argparse error the moment the
    service had already agreed to stop. Every 0.2.0 device went silent that way.
    Only a test that parses the *actual argument list* catches it.
    """

    group("更新助手命令行")
    from observatory_agent import updater
    from observatory_agent.main import PRINT_VERSION, build_parser

    parser = build_parser()

    bare = parser.parse_args(["--version"])
    check("裸 --version 仍打印版本", bare.version == PRINT_VERSION, str(bare.version))

    args = parser.parse_args(
        [
            "--apply-update",
            r"C:\payload",
            "--target",
            r"C:\Program Files\Observatory",
            "--pid",
            "1234",
            "--version-name",
            "0.2.1",
        ]
    )
    check("更新助手参数可解析", args.apply_update == r"C:\payload" and args.version_name == "0.2.1")

    legacy = parser.parse_args(
        ["--apply-update", r"C:\payload", "--target", r"C:\x", "--pid", "1", "--version", "0.2.1"]
    )
    check("兼容旧的 --version 值形式", legacy.version == "0.2.1", str(legacy.version))

    # And prove the spawned argument list itself parses, rather than trusting a
    # hand-written approximation of it.
    captured: dict[str, list[str]] = {}

    original_popen = updater.subprocess.Popen

    class _FakeProcess:
        def __init__(self, arguments) -> None:
            captured["argv"] = list(arguments)

        def poll(self):
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout=None):
            return 0

    updater.subprocess.Popen = lambda arguments, **kwargs: _FakeProcess(arguments)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            helper = updater.spawn_apply_helper(Path(tmp) / "payload", "0.9.9")
    finally:
        updater.subprocess.Popen = original_popen

    argv = captured.get("argv") or []
    check("助手进程已启动", helper is not None)
    check("启动参数使用 --version-name", "--version-name" in argv, str(argv))
    check("启动参数不含裸 --version 带值", "--version" not in argv, str(argv))
    try:
        parsed = parser.parse_args(argv[1:])
        ok = parsed.apply_update.endswith("payload") and parsed.version_name == "0.9.9"
    except SystemExit:
        ok = False
    check("实际启动参数可被解析", ok, str(argv[1:]))

    # A helper that dies instantly must abort the update instead of letting the
    # caller stop the service.
    class _DeadProcess:
        def poll(self):
            return 2

    check("助手立即退出时拒绝升级", updater.verify_helper_alive(_DeadProcess(), grace_s=0.3) is False)

    class _AliveProcess:
        def poll(self):
            return None

    check("助手存活时允许升级", updater.verify_helper_alive(_AliveProcess(), grace_s=0.3) is True)


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    print(f"Observatory 受控端自检 · {version} · Python {sys.version.split()[0]}")
    version_and_config()
    framing()
    transport()
    session_lifecycle()
    large_result_transport()
    helper_cli_contract()
    update_pipeline()
    windows_features()
    uninstall_paths()

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