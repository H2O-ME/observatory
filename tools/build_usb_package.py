#!/usr/bin/env python3
"""Build an offline USB installer folder for the Observatory agent.

Why:每次装机都从服务器下载 26 MB 的受控端包，在慢网络下非常拖。把包直接放到
U 盘里，安装脚本用 ``-Package`` 本地安装，只保留注册/心跳这类必要的网络调用。

Usage::

    python tools/build_usb_package.py --dest "E:/Observatory-USB"
    python tools/build_usb_package.py --dest ... --version 0.3.7 --base-url https://control.tianhw.top

The folder it produces:

    install.ps1              服务端模板渲染版（票据留空，运行时传入）
    usb-install.ps1          读取 tokens.txt + 本地包，调用 install.ps1
    一键安装.cmd             双击入口
    卸载.cmd                 双击卸载
    agent-<ver>.zip          受控端安装包（+ .sha256）
    tokens.txt               粘贴一张部署票据（一行）
    README.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "server" / "observatory" / "templates" / "install.ps1"


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url: str, target: Path) -> str:
    digest = hashlib.sha256()
    tmp = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as response, tmp.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            digest.update(chunk)
            handle.write(chunk)
    tmp.replace(target)
    return digest.hexdigest()


def render_install_ps1(base_urls: list[str], version: str, console_host: str) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    quoted = ", ".join(f"'{url}'" for url in base_urls)
    replacements = {
        "@@VERSION@@": version,
        # 票据留空：由 usb-install.ps1 的 -Token 传入，脚本里不落明文。
        "@@INSTALL_TOKEN@@": "",
        "@@ENROLL_CODE@@": "",
        "@@INSTALL_TOKEN_URL@@": "",
        "@@ENROLL_CODE_URL@@": "",
        "@@BASE_URLS@@": quoted,
        "@@CONSOLE_HOST@@": console_host,
        "@@SERVER_TIME_MS@@": str(int(time.time() * 1000)),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


USB_INSTALL = r"""<#
.SYNOPSIS
    Observatory U 盘离线安装（本地安装包 + 部署票据）。

.DESCRIPTION
    安装包就在本目录：安装时不再从服务器下载受控端，只有"注册/心跳"需要联网。
    票据从 tokens.txt 读取（一张长期票据可反复用于多台机器）。
#>
[CmdletBinding()]
param(
    [int]$EnrollGraceSeconds = 90,
    [switch]$ResetIdentity,
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

$ScriptDir    = $PSScriptRoot
$TokensFile   = Join-Path $ScriptDir 'tokens.txt'
$InstallScript = Join-Path $ScriptDir 'install.ps1'
$UsedFile     = Join-Path $ScriptDir 'used.txt'
$ServiceName  = 'ObservatoryAgent'
$AgentDataDir = Join-Path $env:ProgramData 'Observatory'
$AgentConfig  = Join-Path $AgentDataDir 'config.json'

function Write-Info { param([string]$m) Write-Host "  [$(Get-Date -Format 'HH:mm:ss')] $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  $m" -ForegroundColor Green }
function Write-Warn2{ param([string]$m) Write-Host "  $m" -ForegroundColor Yellow }
function Write-Err2 { param([string]$m) Write-Host "  [错误] $m" -ForegroundColor Red }

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Add-Used {
    param([string]$Note)
    $line = "{0}`t{1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Note
    Add-Content -LiteralPath $UsedFile -Value $line -Encoding ASCII
}

if ($ResetIdentity -and -not $Elevated -and -not (Test-Admin)) {
    Write-Info '重置设备身份需要管理员权限，正在请求 UAC ...'
    $proc = Start-Process -FilePath 'powershell.exe' -Verb RunAs -PassThru -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ("`"{0}`"" -f $PSCommandPath),
        '-ResetIdentity', '-Elevated'
    )
    exit 0
}

Write-Host ''
Write-Host '  ==================================================' -ForegroundColor DarkGray
Write-Host '     Observatory 受控端离线安装（U 盘）' -ForegroundColor White
Write-Host "     目标机 : $env:COMPUTERNAME" -ForegroundColor Gray
Write-Host '  ==================================================' -ForegroundColor DarkGray
Write-Host ''

if (-not (Test-Path -LiteralPath $InstallScript)) {
    Write-Err2 "找不到 install.ps1（应与本脚本同目录）。"
    exit 2
}

$token = @(
    Get-Content -LiteralPath $TokensFile -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and $_ -notlike '#*' }
) | Select-Object -First 1
if (-not $token) {
    Write-Err2 'tokens.txt 里没有部署票据：请在控制台「设置 → 安装」生成一张，'
    Write-Err2 '把整串令牌粘贴到 tokens.txt（一行），再重新双击安装。'
    exit 2
}

$package = Get-ChildItem -LiteralPath $ScriptDir -Filter 'agent-*.zip' -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if ($ResetIdentity -and (Test-Admin)) {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc) { try { Stop-Service -Name $ServiceName -Force -ErrorAction Stop } catch { } }
    Get-Process -Name 'observatory-agent' -ErrorAction SilentlyContinue | ForEach-Object {
        try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch { }
    }
    Start-Sleep -Milliseconds 700
    if (Test-Path -LiteralPath $AgentConfig) {
        Remove-Item -LiteralPath $AgentConfig -Force
        Write-Info '已删除旧设备身份 config.json —— 本机将作为全新设备注册。'
    }
}

$installArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $InstallScript, '-Token', $token)
if ($package) {
    Write-Info "本地安装包: $($package.Name)（安装时不再下载）"
    $installArgs += @('-Package', $package.FullName)
} else {
    Write-Warn2 '本目录没有安装包：回退到从服务器下载（与在线安装完全一致）。'
}

& powershell.exe @installArgs
$rc = $LASTEXITCODE

if ($rc -eq 4) { Write-Err2 'UAC 被取消，安装中止。'; exit 4 }
if ($rc -eq 3) { Write-Err2 '网络不可达（注册需要联网），安装中止。'; exit 3 }
if ($rc -ne 0) { Write-Err2 "安装器退出码 $rc，详见 %TEMP%\observatory-install.log"; exit $rc }

$enrolled = ''
$deadline = (Get-Date).AddSeconds($EnrollGraceSeconds)
while ((Get-Date) -lt $deadline) {
    if (Test-Path -LiteralPath $AgentConfig) {
        try {
            $config = Get-Content -LiteralPath $AgentConfig -Raw | ConvertFrom-Json
            if ($config.token) { $enrolled = [string]$config.device_uid; break }
        } catch { }
    }
    Start-Sleep -Seconds 3
}

if (-not $enrolled) {
    Write-Warn2 ("安装完成但 {0} 秒内未确认注册（受控端会继续自动重试）。" -f $EnrollGraceSeconds)
    Write-Warn2 '若本机曾被删除过设备，需管理员先在服务端处理，再运行：'
    Write-Warn2 '  powershell -ExecutionPolicy Bypass -File usb-install.ps1 -ResetIdentity'
    exit 1
}

Add-Used ("enrolled {0} as {1}" -f $env:COMPUTERNAME, $enrolled)
Write-Ok "安装完成，设备已注册：$enrolled"
Write-Ok "可以直接拔盘装下一台；同一张票据无需更换。"
exit 0
"""

ONE_CLICK_CMD = """@echo off
chcp 936 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0usb-install.ps1"
if errorlevel 1 pause
"""

UNINSTALL_CMD = """@echo off
chcp 936 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Uninstall
if errorlevel 1 pause
"""

README = """Observatory 受控端 U 盘离线安装包
==================================

目录内容
--------
  install.ps1      安装脚本（本地运行，不下载脚本）
  usb-install.ps1  一键安装逻辑（读票据 + 本地包）
  一键安装.cmd      双击这个
  卸载.cmd          双击卸载
  agent-<版本>.zip 受控端安装包（可选；有就用本地，没有就照旧从服务器下载）
  tokens.txt       部署票据（粘贴一行即可，长期复用）

第一次使用
----------
1. 打开控制台 https://control.tianhw.top →「设置 → 安装」→「生成票据」。
2. 复制那串令牌，粘贴到本目录的 tokens.txt（一行，不要引号）。
3. 双击「一键安装.cmd」，点一次 UAC 的「是」。
4. 看到「设备已注册」即完成，可以直接装下一台。

说明
----
* 安装包放在 U 盘里可以跳过 26 MB 下载；删掉 zip 也不影响，脚本会自动
  回退到从服务器下载（与在线安装完全一致）。注册/心跳始终需要联网。
* 同一张票据可装任意多台机器；票据在控制台撤销后立即失效。
* 重装同一台机器不需要换票据：设备 uid 由 Windows MachineGuid 派生，保持不变。
* 若本机曾在控制台被删除过，注册会被拒绝，需管理员处理后运行：
    powershell -ExecutionPolicy Bypass -File usb-install.ps1 -ResetIdentity
* 升级：服务器推送的新版本仍然自动更新，无需重新装机。
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the offline USB installer folder")
    parser.add_argument("--dest", required=True, help="目标目录（例如 E:/Observatory-USB）")
    parser.add_argument("--base-url", default="https://control.tianhw.top", help="控制台地址")
    parser.add_argument("--version", default="", help="指定受控端版本（默认取服务器 latest）")
    parser.add_argument("--keep-tokens", action="store_true", help="保留目标目录里已有的 tokens.txt")
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    bootstrap = fetch_json(f"{base}/api/v1/public/bootstrap")
    latest = fetch_json(f"{base}/api/v1/public/agent/latest")
    if not latest.get("available"):
        print("服务器没有可下载的受控端安装包，先在控制台上传一个。", file=sys.stderr)
        return 2

    version = args.version or str(latest["version"])
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    zip_path = dest / f"agent-{version}.zip"
    url = f"{base}/api/v1/public/agent/{version}/package"
    print(f"下载 {version} …")
    actual = download(url, zip_path)
    expected = str(latest.get("sha256") or "").lower()
    if expected and actual != expected:
        zip_path.unlink(missing_ok=True)
        print(f"sha256 校验失败：期望 {expected}，实际 {actual}", file=sys.stderr)
        return 3
    (dest / f"agent-{version}.zip.sha256").write_text(f"{actual}  agent-{version}.zip\n", encoding="utf-8")
    print(f"安装包已保存：{zip_path}（{actual[:16]}…）")

    for stale in dest.glob("agent-*.zip"):
        if stale != zip_path:
            stale.unlink(missing_ok=True)
            stale.with_suffix(stale.suffix + ".sha256").unlink(missing_ok=True)

    base_urls = list(bootstrap.get("base_urls") or [base])
    console_host = str(bootstrap.get("console_host") or "control.tianhw.top")
    (dest / "install.ps1").write_text(
        render_install_ps1(base_urls, version, console_host), encoding="utf-8-sig", newline="\r\n"
    )
    (dest / "usb-install.ps1").write_text(USB_INSTALL, encoding="utf-8-sig", newline="\r\n")
    (dest / "一键安装.cmd").write_text(ONE_CLICK_CMD, encoding="ascii", newline="\r\n")
    (dest / "卸载.cmd").write_text(UNINSTALL_CMD, encoding="ascii", newline="\r\n")
    (dest / "README.txt").write_text(README, encoding="utf-8-sig", newline="\r\n")

    tokens = dest / "tokens.txt"
    if not (args.keep_tokens and tokens.exists()):
        tokens.write_text(
            "# 把控制台「设置 → 安装 → 生成票据」得到的整串令牌粘贴到这里（一行）。\n",
            encoding="utf-8-sig",
        )

    print(f"已生成 U 盘安装包：{dest}")
    for item in sorted(dest.iterdir()):
        print(f"  {item.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
