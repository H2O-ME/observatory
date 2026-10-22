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

        # Pre-flight: a payload whose executable cannot start must not cost the
        # running install anything. b"not-a-pe-image" is not a real PE file, so
        # running it fails (WinError 193 on Windows / EACCES on POSIX) -- which is
        # exactly the "bad build downloaded" case this guard exists for.
        broken = work / "broken-build"
        broken.mkdir()
        (broken / "observatory-agent.exe").write_bytes(b"not-a-pe-image")
        code_broken = updater.apply_now(str(broken), str(target), 0, "9.9.9")
        check("坏包不通过预检", code_broken == 4, str(code_broken))
        check("坏包不破坏现有安装", (target / "observatory-agent.exe").read_bytes() == b"MZ-0.2.0")

        # The swap target has to be the install root (the directory holding the
        # exe). An INSTALL_DIR resolving to _internal would rename the support
        # directory and leave the exe with no Python runtime next to it.
        wrong_root = work / "wrong-root"
        (wrong_root / "_internal").mkdir(parents=True)
        code_wrong = updater.apply_now(str(payload), str(wrong_root / "_internal"), 0, "0.2.1")
        check("拒绝把 _internal 当作安装根目录", code_wrong == 7, str(code_wrong))
        check("拒绝后不动那个目录", (wrong_root / "_internal").is_dir())

        calls = {"stopped": 0, "started": 0, "cleaned": 0}
        original = (
            updater._wait_for_process_exit,
            updater._stop_service,
            updater._start_service,
            updater._cleanup_old,
            updater.verify_payload,
        )
        updater._wait_for_process_exit = lambda *a, **k: None
        updater._stop_service = lambda *a, **k: calls.__setitem__("stopped", calls["stopped"] + 1)
        updater._start_service = lambda *a, **k: (calls.__setitem__("started", calls["started"] + 1), True)[1]
        updater._cleanup_old = lambda *a, **k: calls.__setitem__("cleaned", calls["cleaned"] + 1)
        updater.verify_payload = lambda *a, **k: (True, "0.2.1")
        try:
            code = updater.apply_now(str(payload), str(target), 0, "0.2.1")
        finally:
            (
                updater._wait_for_process_exit,
                updater._stop_service,
                updater._start_service,
                updater._cleanup_old,
                updater.verify_payload,
            ) = original

        check("更新助手返回成功", code == 0, str(code))
        check("新版本已就位", (target / "observatory-agent.exe").read_bytes() == b"MZ-0.2.1")
        check("旧版本文件不再存在", not (target / "stale.txt").exists())
        check("更新前先停服务", calls["stopped"] == 1, str(calls))
        check("更新后重启服务", calls["started"] == 1, str(calls))
        check("清理旧目录", calls["cleaned"] == 1, str(calls))

        # Swap succeeds but the new build refuses to come up as a service: the
        # helper has to put the previous build back, otherwise the host is left
        # with a stopped service and nothing to restart it.
        payload2 = work / "new-build-2"
        payload2.mkdir()
        (payload2 / "observatory-agent.exe").write_bytes(b"MZ-0.2.3")
        target2 = work / "install2"
        target2.mkdir()
        (target2 / "observatory-agent.exe").write_bytes(b"MZ-0.2.2")

        starts = {"n": 0}

        def start_stub():
            starts["n"] += 1
            return starts["n"] > 1  # new build fails, rolled-back build works

        original2 = (updater._stop_service, updater._start_service, updater.verify_payload)
        updater._stop_service = lambda *a, **k: None
        updater._start_service = start_stub
        updater.verify_payload = lambda *a, **k: (True, "0.2.3")
        try:
            code2 = updater.apply_now(str(payload2), str(target2), 0, "0.2.3")
        finally:
            (updater._stop_service, updater._start_service, updater.verify_payload) = original2

        check("新版本起不来时回滚", code2 == 5, str(code2))
        check("回滚后旧版本仍在位", (target2 / "observatory-agent.exe").read_bytes() == b"MZ-0.2.2")
        check("回滚共尝试两次启动", starts["n"] == 2, str(starts))

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
    # 会话 65537 是监听会话、没有登录用户，不能算进可采集会话数——否则会出现
    # "1 个桌面、2 个会话"这种和 state 自相矛盾的报告。
    check("会话数只统计有登录用户的会话", health["sessions"] == 1, str(health))
    check("监听会话不计入会话数", 65537 in runner._sessions and health["sessions"] != len(runner._sessions), str(health))


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
        def __init__(self, arguments, **kwargs) -> None:
            captured["argv"] = list(arguments)
            captured["cwd"] = kwargs.get("cwd")

        def poll(self):
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout=None):
            return 0

    updater.subprocess.Popen = lambda arguments, **kwargs: _FakeProcess(arguments, **kwargs)
    staging_before = updater.STAGING_DIR
    staged_ok = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            updater.STAGING_DIR = root / "staging"
            payload = root / "payload"
            (payload / "_internal").mkdir(parents=True)
            (payload / updater.EXE_NAME).write_bytes(b"MZ")
            (payload / "_internal" / "lib.dll").write_bytes(b"dll")
            helper = updater.spawn_apply_helper(payload, "0.9.9")
            # Inspect while the temporary directory still exists.
            argv_now = captured.get("argv") or []
            helper_path = Path(argv_now[0]) if argv_now else Path("?")
            staged_cwd = str(captured.get("cwd") or "")
            staged_ok = {
                "internal": (helper_path.parent / "_internal" / "lib.dll").is_file(),
                "exe": helper_path.is_file(),
                "not_payload": "payload" not in helper_path.parts,
                "cwd": bool(staged_cwd) and Path(staged_cwd).is_dir(),
                "path": str(helper_path),
                "cwd_path": staged_cwd,
            }
    finally:
        updater.subprocess.Popen = original_popen
        updater.STAGING_DIR = staging_before

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

    # A PyInstaller onedir build only runs next to its _internal directory, and the
    # helper cannot run from the payload directory because applying the update
    # renames it. Both constraints must hold in the spawned command.
    check("助手目录携带 _internal", staged_ok.get("internal") is True, str(staged_ok.get("path")))
    check("助手可执行文件存在", staged_ok.get("exe") is True, str(staged_ok.get("path")))
    check("助手不在 payload 目录内运行", staged_ok.get("not_payload") is True, str(staged_ok.get("path")))
    check("助手工作目录已就位", staged_ok.get("cwd") is True, str(staged_ok.get("cwd_path")))

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


def update_retry_and_cleanup() -> None:
    """A failing update must not re-download the package on every heartbeat."""

    group("更新重试与清理")
    import asyncio
    import time as time_module

    from observatory_agent import runner as runner_module
    from observatory_agent import updater

    runner = runner_module.AgentRunner.__new__(runner_module.AgentRunner)
    runner._updating = False
    runner._connected = True
    runner._update_failures = {}
    runner.client = object()
    runner.store = type("S", (), {"config": type("C", (), {"setting": staticmethod(lambda *a, **k: True)})()})()

    events: list[tuple[str, dict]] = []

    async def fake_event(name, payload):
        events.append((name, dict(payload)))

    runner._send_event = fake_event
    runner._request_shutdown = lambda reason: None

    calls = {"apply": 0}

    async def failing_apply(**kwargs):
        calls["apply"] += 1
        raise RuntimeError("助手启动即退出")

    original_apply = updater.apply_update
    updater.apply_update = failing_apply
    try:
        info = {"version": "9.9.9", "sha256": "x" * 64}
        asyncio.run(runner_module.AgentRunner._maybe_update(runner, info))
        first = calls["apply"]
        # Immediately asking again must be suppressed by the backoff.
        asyncio.run(runner_module.AgentRunner._maybe_update(runner, info))
        second = calls["apply"]
    finally:
        updater.apply_update = original_apply

    check("首次失败后记录退避", first == 1 and second == 1, f"{first}/{second}")
    check("失败事件带尝试次数", any(p.get("attempt") == 1 for n, p in events if n == "update_failed"), str(events))
    check("退避窗口内不重试", second == 1, str(calls))

    # Once the backoff expires the retry is allowed again.
    failures = runner._update_failures["9.9.9"]
    runner._update_failures["9.9.9"] = (failures[0], time_module.monotonic() - 10_000)
    updater.apply_update = failing_apply
    try:
        asyncio.run(runner_module.AgentRunner._maybe_update(runner, {"version": "9.9.9"}))
    finally:
        updater.apply_update = original_apply
    check("退避到期后允许重试", calls["apply"] == 2, str(calls))

    # Staging cleanup must drop leftovers, including the legacy helper that can
    # never run from an onedir build.
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        (staging / "payload-0.2.4").mkdir()
        (staging / "payload-0.2.4" / "x.dll").write_bytes(b"x")
        (staging / "apply-1-2").mkdir()
        (staging / "observatory-updater.exe").write_bytes(b"MZ")
        (staging / "observatory-agent-0.2.4.zip").write_bytes(b"PK")
        (staging / "keep.part").write_bytes(b"p")
        updater.cleanup_staging(staging)
        left = sorted(p.name for p in staging.iterdir())
        check("清理遗留载荷", not (staging / "payload-0.2.4").exists(), str(left))
        check("清理助手工作区", not (staging / "apply-1-2").exists(), str(left))
        check("清理坏助手副本", not (staging / "observatory-updater.exe").exists(), str(left))
        check("清理旧压缩包", not (staging / "observatory-agent-0.2.4.zip").exists(), str(left))
        check("清理半成品", not (staging / "keep.part").exists(), str(left))


def transport_upgrade() -> None:
    """缺陷 1 回归：WebSocket 探测成功后必须真的切换过去。

    旧代码里 ``_upgrade_probe`` 只改 ``self.level`` 并调用 ``PollChannel.close()``，
    而那是继承来的 no-op；轮询的 ``while True`` 不感知关闭，``_network_loop``
    一直阻塞在 ``await run_task``，等轮询自己失败后又把级别降回 short-poll。
    这个用例断言：探测成功后确实 cancel 了轮询通道、以 LEVEL_WS 建新通道，
    并且 WS 真的不可用时仍会降级。
    """

    group("传输升级")
    import asyncio
    import threading
    from types import SimpleNamespace

    from observatory_agent import runner as runner_module
    from observatory_agent.transport import LEVEL_POLL_LONG, LEVEL_POLL_SHORT, LEVEL_WS

    def make_runner(channels_state: dict, probe_ok: bool, stopping: threading.Event):
        runner = runner_module.AgentRunner.__new__(runner_module.AgentRunner)
        runner.store = SimpleNamespace(config=SimpleNamespace(setting=lambda *a, **k: 20))
        runner.client = object()
        runner._stopping = stopping
        runner._stop_event = asyncio.Event()
        runner._upgrade_requested = asyncio.Event()
        runner.level = channels_state["start_level"]
        runner._connected = False
        runner.channel = None
        runner._pipes = {}
        runner._processes = {}
        runner._sessions = {}
        runner._launch_attempts = {}

        async def enrolled():
            return True

        async def report(_stage):
            return None

        async def probe():
            return probe_ok

        runner._ensure_enrolled = enrolled
        runner._report_state = report
        runner._probe_ws = probe

        original_sleep = runner_module.AgentRunner._sleep_or_stop

        async def recording_sleep(seconds):
            channels_state["sleeps"].append(float(seconds))
            await original_sleep(runner, seconds)

        runner._sleep_or_stop = recording_sleep
        return runner

    class FakeChannel:
        def __init__(self, level, state):
            self.level = level
            self.kind = "ws" if level == LEVEL_WS else "poll"
            self.ready = asyncio.Event()
            self.cancelled = False
            self.state = state

        async def run(self, handler, heartbeat_s):
            try:
                self.ready.set()
                if self.level == LEVEL_WS:
                    self.state["ws_ready"].set()
                    await self.state["ws_end"].wait()
                    raise RuntimeError("测试主动断开 websocket")
                self.state["poll_ready"].set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def close(self):
            return None

    def install(state, start_level):
        state.update(
            {
                "start_level": start_level,
                "channels": [],
                "levels": [],
                "sleeps": [],
                "ws_ready": asyncio.Event(),
                "ws_end": asyncio.Event(),
                "poll_ready": asyncio.Event(),
            }
        )

        original_build = runner_module.build_channel
        original_probe_delay = runner_module.UPGRADE_PROBE_BASE_S
        original_backoff = runner_module.RECONNECT_BACKOFF_S

        def fake_build(level, client, provider):
            channel = FakeChannel(level, state)
            state["channels"].append(channel)
            state["levels"].append(level)
            return channel

        runner_module.build_channel = fake_build
        # 探测间隔和重连退避都缩短到毫秒级，测试不需要真的等两分钟。
        runner_module.UPGRADE_PROBE_BASE_S = 0.01
        runner_module.RECONNECT_BACKOFF_S = (0.05, 0.05)

        def restore():
            runner_module.build_channel = original_build
            runner_module.UPGRADE_PROBE_BASE_S = original_probe_delay
            runner_module.RECONNECT_BACKOFF_S = original_backoff

        return restore

    async def scenario_upgrade() -> dict:
        state: dict = {}
        stopping = threading.Event()
        restore = install(state, LEVEL_POLL_SHORT)
        runner = make_runner(state, probe_ok=True, stopping=stopping)
        task = asyncio.create_task(runner._network_loop())
        result = dict(state)
        result["timeout"] = False
        try:
            await asyncio.wait_for(state["ws_ready"].wait(), timeout=5)
            result["level_after_upgrade"] = runner.level
            result["levels_seen"] = list(state["levels"])
            result["poll_cancelled"] = state["channels"][0].cancelled
            result["sleeps"] = list(state["sleeps"])
            # 先置停止位再放行 WS，让循环走正常退出路径而不是被 cancel。
            stopping.set()
            state["ws_end"].set()
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            result["timeout"] = True
        finally:
            stopping.set()
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            restore()
        result["pending"] = [
            item for item in asyncio.all_tasks() if item is not asyncio.current_task() and not item.done()
        ]
        return result

    upgrade = asyncio.run(scenario_upgrade())
    check("升级后确实建立了 WebSocket 通道", not upgrade["timeout"], str(upgrade.get("levels_seen")))
    check(
        "升级路径不经过任何降级级别",
        upgrade.get("levels_seen") == [LEVEL_POLL_SHORT, LEVEL_WS],
        str(upgrade.get("levels_seen")),
    )
    check("升级时轮询通道被主动取消", upgrade.get("poll_cancelled") is True, str(upgrade.get("poll_cancelled")))
    check("升级后级别保持为 ws", upgrade.get("level_after_upgrade") == LEVEL_WS, str(upgrade.get("level_after_upgrade")))
    check("升级不按断线退避等待", 0.05 not in upgrade.get("sleeps", []), str(upgrade.get("sleeps")))
    check("升级过程不泄漏 task", not upgrade.get("pending"), str(upgrade.get("pending")))

    async def scenario_degrade() -> dict:
        state: dict = {}
        stopping = threading.Event()
        restore = install(state, LEVEL_WS)
        runner = make_runner(state, probe_ok=False, stopping=stopping)

        # WS 建连即失败，必须按阶梯降到 long-poll。
        class FailingWs(FakeChannel):
            async def run(self, handler, heartbeat_s):
                raise RuntimeError("websocket 不可用")

        install_build = runner_module.build_channel

        def fake_build(level, client, provider):
            if level == LEVEL_WS:
                state["levels"].append(level)
                return FailingWs(level, state)
            return install_build(level, client, provider)

        runner_module.build_channel = fake_build
        task = asyncio.create_task(runner._network_loop())
        result = {"timeout": False}
        try:
            await asyncio.wait_for(state["poll_ready"].wait(), timeout=5)
            result["levels_seen"] = list(state["levels"])
            result["level_after_degrade"] = runner.level
        except asyncio.TimeoutError:
            result["timeout"] = True
            result["levels_seen"] = list(state["levels"])
        finally:
            stopping.set()
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            restore()
        result["pending"] = [
            item for item in asyncio.all_tasks() if item is not asyncio.current_task() and not item.done()
        ]
        return result

    degrade = asyncio.run(scenario_degrade())
    check("WS 不可用时降级到 long-poll", not degrade["timeout"] and degrade["level_after_degrade"] == LEVEL_POLL_LONG, str(degrade))
    check("降级阶梯未被破坏", degrade.get("levels_seen") == [LEVEL_WS, LEVEL_POLL_LONG], str(degrade.get("levels_seen")))
    check("降级过程不泄漏 task", not degrade.get("pending"), str(degrade.get("pending")))


def ghost_session_backoff() -> None:
    """缺陷 2 回归：没有登录用户的幽灵会话不再无限快速重试。

    旧代码在 5 次失败后把 ``_launch_attempts`` 清零，下一次扫描（5 秒后）因为
    ``attempts == 0`` 直接跳过冷却，等于 30 秒一轮的死循环；同时 heartbeat 的
    ``capture`` 依赖这里的失败信息，修复不能把可观测性一起吞掉。
    """

    group("幽灵会话退避")
    import asyncio
    import threading
    import time as time_module

    from observatory_agent import runner as runner_module
    from observatory_agent import sysinfo as sysinfo_module

    # -- 1) 没有登录用户名的会话不拉起 worker，但仍如实上报 no_session -------
    runner = runner_module.AgentRunner.__new__(runner_module.AgentRunner)
    runner._sessions = {}
    runner._pipes = {}
    runner._processes = {}
    runner._launch_attempts = {}
    launches: list[int] = []
    runner._ensure_worker = lambda session_id: launches.append(session_id)

    original_list = sysinfo_module.list_sessions
    original_console = sysinfo_module.console_session_id
    sysinfo_module.list_sessions = lambda: [
        {"session_id": 1, "username": "", "state": "active", "is_console": False},
        {"session_id": 2, "username": "Administrator", "state": "active", "is_console": True},
    ]
    sysinfo_module.console_session_id = lambda: 2
    try:
        asyncio.run(runner_module.AgentRunner._scan_sessions(runner))
    finally:
        sysinfo_module.list_sessions = original_list
        sysinfo_module.console_session_id = original_console

    check("没有登录用户的会话不拉起采屏进程", launches == [2], str(launches))
    check("幽灵会话被标记为不可采集", runner._sessions.get(1, {}).get("connectable") is False, str(runner._sessions.get(1)))
    check("真实桌面会话仍会拉起", runner._sessions.get(2, {}).get("connectable") is True, str(runner._sessions.get(2)))

    only_ghost = runner_module.AgentRunner.__new__(runner_module.AgentRunner)
    only_ghost._pipes = {}
    only_ghost._sessions = {1: {"username": "", "connectable": False}}
    health = runner_module.AgentRunner._capture_health(only_ghost)
    check("只有幽灵会话时上报 no_session", health["state"] == "no_session", str(health))
    check("no_session 带可读原因", "桌面" in health["reason"] or "采集" in health["reason"], health["reason"])

    # -- 2) 拉起失败使用递进退避，且连接成功后重置 --------------------------
    worker = runner_module.AgentRunner.__new__(runner_module.AgentRunner)
    worker.service_mode = True
    worker._sessions = {}
    worker._processes = {}
    worker._launch_attempts = {}

    class FakePipe:
        def __init__(self) -> None:
            self.connected = threading.Event()

    pipe = FakePipe()
    worker._pipes = {7: pipe}

    calls = {"launch": 0}
    original_launch = runner_module.launch_worker

    def failing_launch(session_id):
        calls["launch"] += 1
        return None

    runner_module.launch_worker = failing_launch
    try:
        worker._ensure_worker(7)
        first_attempts = worker._launch_attempts.get(7, (0, 0.0))[0]
        check("拉起失败记录失败次数", first_attempts == 1, str(worker._launch_attempts))
        # 冷却窗口内立即再扫描一次，不能重试。
        worker._ensure_worker(7)
        check("冷却窗口内不重试", calls["launch"] == 1, str(calls))

        # 模拟 30 秒冷却到期：失败次数必须保留并继续递增。
        worker._launch_attempts[7] = (1, time_module.monotonic() - 31.0)
        worker._ensure_worker(7)
        check("退避到期后重试且次数递增", calls["launch"] == 2 and worker._launch_attempts[7][0] == 2, str(worker._launch_attempts))

        # 连续失败 5 次以后：旧代码在这里清零并立即重来；新代码必须保留计数、
        # 按长退避重试（上限 30 分钟）。
        worker._launch_attempts[7] = (
            runner_module.LAUNCH_RETRIES,
            time_module.monotonic() - runner_module.LAUNCH_BACKOFF_MAX_S - 1,
        )
        before = calls["launch"]
        worker._ensure_worker(7)
        check("达到快速重试上限后仍按长退避重试", calls["launch"] == before + 1, str(calls))
        check(
            "达到上限后失败次数不被清零",
            worker._launch_attempts[7][0] == runner_module.LAUNCH_RETRIES + 1,
            str(worker._launch_attempts),
        )

        # 长退避窗口内反复扫描（模拟每 5 秒一次的 session scan）不得再拉起。
        worker._launch_attempts[7] = (6, time_module.monotonic())
        before = calls["launch"]
        for _ in range(10):
            worker._ensure_worker(7)
        check("长退避窗口内反复扫描不重试", calls["launch"] == before, str(calls))

        # 可观测性：失败期间 capture 仍如实报告 blocked 及原因。
        blocked = runner_module.AgentRunner._capture_health(worker)
        check(
            "拉起失败时 capture 上报 blocked",
            blocked["state"] == "blocked" and "屏幕采集" in blocked["reason"],
            str(blocked),
        )

        check(
            "退避按 30s→60s→… 递进",
            [
                worker._launch_backoff(1),
                worker._launch_backoff(2),
                worker._launch_backoff(3),
                worker._launch_backoff(4),
            ]
            == [30.0, 60.0, 120.0, 240.0],
            str([worker._launch_backoff(n) for n in (1, 2, 3, 4)]),
        )
        check("退避上限 30 分钟", worker._launch_backoff(99) == 1800.0, str(worker._launch_backoff(99)))

        # 连接成功后必须重置退避状态。
        pipe.connected.set()
        worker._ensure_worker(7)
        check("连上后重置退避", 7 not in worker._launch_attempts, str(worker._launch_attempts))
        check("连上后清除阻塞标记", "launch_error" not in worker._sessions.get(7, {}), str(worker._sessions.get(7)))
    finally:
        runner_module.launch_worker = original_launch


def install_dir_resolution() -> None:
    """The frozen layout must not resolve INSTALL_DIR to ``_internal``.

    Getting this wrong is how a device bricked itself: the updater swapped
    ``<install>\\_internal`` instead of the install root, the exe stayed at the
    root with no Python runtime beside it, and the service could not start.
    """
    group("安装目录解析（冻结布局不能落到 _internal）")
    from pathlib import Path as _Path

    from observatory_agent import config as agent_config

    source_dir = agent_config.install_dir()
    check("源码布局下指向 agent 目录", source_dir.name == "agent", str(source_dir))

    saved_frozen = getattr(sys, "frozen", None)
    saved_exe = sys.executable
    try:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            install_root = _Path(tmp) / "Observatory"
            sys.frozen = True
            sys.executable = str(install_root / "observatory-agent.exe")
            frozen_dir = agent_config.install_dir()
            check("冻结布局下指向 exe 所在目录", frozen_dir == install_root, str(frozen_dir))
            check("冻结布局下不会解析到 _internal", frozen_dir.name != "_internal", str(frozen_dir))
            check(
                "冻结布局下与 exe 同级（onedir 的 _internal 在它下面）",
                frozen_dir.parent == install_root.parent,
                str(frozen_dir),
            )
    finally:
        if saved_frozen is None:
            if hasattr(sys, "frozen"):
                del sys.frozen
        else:
            sys.frozen = saved_frozen
        sys.executable = saved_exe

    saved_override = os.environ.get("OBS_INSTALL_DIR")
    try:
        os.environ["OBS_INSTALL_DIR"] = str(_Path("/tmp/explicit-install"))
        check("OBS_INSTALL_DIR 覆盖优先", agent_config.install_dir() == _Path("/tmp/explicit-install"))
    finally:
        if saved_override is None:
            os.environ.pop("OBS_INSTALL_DIR", None)
        else:
            os.environ["OBS_INSTALL_DIR"] = saved_override


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    print(f"Observatory 受控端自检 · {version} · Python {sys.version.split()[0]}")
    version_and_config()
    install_dir_resolution()
    framing()
    transport()
    transport_upgrade()
    session_lifecycle()
    ghost_session_backoff()
    large_result_transport()
    helper_cli_contract()
    update_retry_and_cleanup()
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