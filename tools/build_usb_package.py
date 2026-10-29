#!/usr/bin/env python3
"""Build an offline USB installer folder for the Observatory agent.

为什么需要它
------------
受控端包有 26 MB，每次装机都从服务器下载在慢网络下非常拖。把包放到 U 盘里，
安装脚本直接用它，只保留注册/心跳这类必要的网络调用（那是必须联网的部分）。

0.3.8 起 ``install.ps1`` **自己会找同目录的安装包**（含 ``package\\`` 子目录，
甚至能从旧的 ``Observatory-Setup.zip`` 里抽出内层 agent 包），所以生成的 U 盘
目录不再需要额外的一层包装脚本。

Usage::

    python tools/build_usb_package.py --dest "E:/Observatory-USB"
    python tools/build_usb_package.py --dest ... --version 0.3.8 --base-url https://control.tianhw.top
    python tools/build_usb_package.py --dest ... --package D:/dist/observatory-agent-0.3.8.zip

The folder it produces:

    install.ps1               服务端模板渲染版（自动发现本地包；带下载进度条）
    observatory-agent-x.zip   受控端安装包（+ .sha256）
    一键安装.cmd              双击安装（自动用本地包，不下载）
    在线安装.cmd              忽略本地包，强制从服务器下载（含进度条）
    卸载.cmd                  双击卸载
    README.txt                给现场的人看的说明

注册不需要任何票据：受控端首启自动注册，U 盘只解决"安装包不用重新下载"。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "server" / "observatory" / "templates" / "install.ps1"
DEFAULT_BASE_URL = "https://control.tianhw.top"


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url: str, target: Path) -> str:
    """流式下载并回显进度（U 盘制作也要能看出是不是卡住了）。"""

    digest = hashlib.sha256()
    tmp = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as response, tmp.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        started = time.monotonic()
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            digest.update(chunk)
            handle.write(chunk)
            received += len(chunk)
            elapsed = max(time.monotonic() - started, 1e-6)
            speed = received / elapsed
            if total:
                percent = int(received * 100 / total)
                eta = (total - received) / speed if speed > 1 else 0
                sys.stdout.write(
                    f"\r  {percent:3d}%  {received / 1048576:6.1f} / {total / 1048576:.1f} MB"
                    f"  {speed / 1048576:5.1f} MB/s  剩余 {eta:5.1f}s "
                )
            else:
                sys.stdout.write(f"\r  {received / 1048576:6.1f} MB  {speed / 1048576:5.1f} MB/s ")
            sys.stdout.flush()
    sys.stdout.write("\n")
    tmp.replace(target)
    return digest.hexdigest()


def render_install_ps1(base_urls: list[str], version: str, console_host: str) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    quoted = ", ".join(f"'{url}'" for url in base_urls)
    replacements = {
        "@@VERSION@@": version,
        "@@BASE_URLS@@": quoted,
        "@@CONSOLE_HOST@@": console_host,
        "@@SERVER_TIME_MS@@": str(int(time.time() * 1000)),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    if "@@" in text:
        raise SystemExit("模板里仍有未替换的占位符，渲染结果不可用")
    return text


ONE_CLICK_CMD = """@echo off
chcp 936 >nul
cd /d "%~dp0"
title Observatory offline install
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Offline
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo   Install did not finish. Exit code %RC%.
  echo   Log: %TEMP%\\observatory-install.log
  echo.
)
pause
exit /b %RC%
"""

ONLINE_CMD = """@echo off
chcp 936 >nul
cd /d "%~dp0"
title Observatory online install
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Online
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo   Install did not finish. Exit code %RC%.
  echo   Log: %TEMP%\\observatory-install.log
  echo.
)
pause
exit /b %RC%
"""

UNINSTALL_CMD = """@echo off
chcp 936 >nul
cd /d "%~dp0"
title Observatory uninstall
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Uninstall
echo.
pause
"""

README = """Observatory 受控端 U 盘离线安装包
==================================

目录内容
--------
  install.ps1                 安装脚本（自动发现本目录的安装包，不联网下载）
  observatory-agent-<版本>.zip 受控端安装包
  一键安装.cmd                 双击这个（默认用本地包）
  在线安装.cmd                 从服务器下载安装包（会显示下载进度条）
  卸载.cmd                     双击卸载本机的受控端

第一次使用
----------
1. 双击「一键安装.cmd」，点一次 UAC 的「是」。
2. 看到「安装完成 / 设备标识 OBS-xxxx」即完成，可以直接拔盘装下一台。

怎么知道它是真的没联网下载
--------------------------
安装过程里如果没出现「下载中 xx%」，说明用的是 U 盘里的包。「一键安装.cmd」
带 -Offline：找不到本地包会直接报错退出（退出码 5），绝不会偷偷联网 —— 所以
只要它装成功了，就是用本地包装的。

需要联网的部分
--------------
注册和心跳必须能连到服务器（https://control.tianhw.top）。受控端包本身不用
下载；如果你在完全隔离的内网机器上装，装是能装完的，但要等这台机器能访问
控制台之后才会出现在设备列表里（受控端会一直自动重试）。

常见问题
--------
* 提示「需要一次管理员授权」：点「是」。整个生命周期只有这一次 UAC，
  之后自动更新、远程卸载、远程控制都不再弹。
* 提示「安装包里找不到 observatory-agent.exe」：U 盘里的 zip 不完整（拷贝
  中断）。用「在线安装.cmd」或者重新拷一次包。
* 想看详细日志：%TEMP%\\observatory-install.log
* 重复安装同一台机器不需要任何额外操作（设备 uid 由 MachineGuid 派生）。
* 装完还想升级：不需要再插 U 盘，服务器会自动推送新版本。
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the offline USB installer folder")
    parser.add_argument("--dest", required=True, help="目标目录（例如 E:/Observatory-USB）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="控制台地址（多线路从 bootstrap 自动取）")
    parser.add_argument("--version", default="", help="指定受控端版本（默认取服务器 latest）")
    parser.add_argument("--package", default="", help="直接用本地 zip，不下载（构建机没有网络时用）")
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    version = args.version
    base_urls = [base]
    console_host = base.split("//", 1)[-1].split("/", 1)[0]
    local_zip = Path(args.package).expanduser() if args.package else None

    if local_zip is not None:
        if not local_zip.is_file():
            print(f"找不到本地安装包: {local_zip}", file=sys.stderr)
            return 2
        if not version:
            version = next((part for part in local_zip.stem.split("-") if part.count(".") == 2), "")
        if not version:
            print("无法从文件名推断版本，请加 --version", file=sys.stderr)
            return 2
        try:
            bootstrap = fetch_json(f"{base}/api/v1/public/bootstrap")
            base_urls = list(bootstrap.get("base_urls") or [base])
            console_host = str(bootstrap.get("console_host") or console_host)
        except OSError as exc:
            print(f"取服务器地址失败（用 --base-url 兜底）: {exc}", file=sys.stderr)
        digest = hashlib.sha256(local_zip.read_bytes()).hexdigest()
        zip_path = dest / f"observatory-agent-{version}.zip"
        shutil.copyfile(local_zip, zip_path)
        print(f"已复制本地安装包：{zip_path}")
    else:
        bootstrap = fetch_json(f"{base}/api/v1/public/bootstrap")
        latest = fetch_json(f"{base}/api/v1/public/agent/latest")
        if not latest.get("available"):
            print("服务器没有可下载的受控端安装包，先在控制台上传一个。", file=sys.stderr)
            return 2
        version = version or str(latest["version"])
        zip_path = dest / f"observatory-agent-{version}.zip"
        url = f"{base}/api/v1/public/agent/{version}/package"
        print(f"下载 {version} …")
        digest = download(url, zip_path)
        expected = str(latest.get("sha256") or "").lower()
        if expected and expected != digest and str(latest["version"]) == version:
            zip_path.unlink(missing_ok=True)
            print(f"sha256 校验失败：期望 {expected}，实际 {digest}", file=sys.stderr)
            return 3
        base_urls = list(bootstrap.get("base_urls") or [base])
        console_host = str(bootstrap.get("console_host") or console_host)
        print(f"安装包已保存：{zip_path}（sha256 {digest[:16]}…）")

    if not version:
        version = "0.0.0"
    (dest / f"{zip_path.name}.sha256").write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    print(f"  已写校验文件：{zip_path.name}.sha256")

    for stale in dest.glob("*.zip"):
        if stale != zip_path:
            stale.unlink(missing_ok=True)
            Path(str(stale) + ".sha256").unlink(missing_ok=True)
            print(f"  清掉旧包：{stale.name}")
    for stale in dest.glob("agent-*.zip*"):
        stale.unlink(missing_ok=True)

    (dest / "install.ps1").write_text(
        render_install_ps1(base_urls, version, console_host), encoding="utf-8-sig", newline="\r\n"
    )
    (dest / "一键安装.cmd").write_text(ONE_CLICK_CMD, encoding="ascii", newline="\r\n")
    (dest / "在线安装.cmd").write_text(ONLINE_CMD, encoding="ascii", newline="\r\n")
    (dest / "卸载.cmd").write_text(UNINSTALL_CMD, encoding="ascii", newline="\r\n")
    (dest / "README.txt").write_text(README, encoding="utf-8-sig", newline="\r\n")

    # 旧版 U 盘包里的文件不再需要（凭据、包装脚本、旧 bundle）。
    for legacy in ("tokens.txt", "used.txt", "usb-install.ps1", "Observatory-Setup.zip", "Install-Observatory.ps1"):
        stale = dest / legacy
        if stale.exists():
            stale.unlink()
            print(f"  清掉旧版文件：{legacy}")

    total = sum(item.stat().st_size for item in dest.iterdir() if item.is_file())
    print(f"\n已生成 U 盘安装包：{dest}（{total / 1048576:.1f} MB，版本 {version}）")
    for item in sorted(dest.iterdir()):
        print(f"  {item.name:34s} {item.stat().st_size / 1024:8.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
