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


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    print(f"Observatory 受控端自检 · {version} · Python {sys.version.split()[0]}")
    version_and_config()
    framing()
    transport()
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