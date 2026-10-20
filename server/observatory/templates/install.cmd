@echo off
rem Observatory 受控端安装程序 —— 双击运行即可。
rem
rem 这个文件由服务器按请求生成：
rem   * 已内置注册码与版本号，不需要手输任何参数
rem   * 内置多个服务器地址，下载安装脚本时逐个尝试（多线路热备）
rem   * 真正的安装逻辑在 install.ps1 里，由它完成一次 UAC 授权后静默安装
rem
rem 卸载：在本目录执行  "C:\Program Files\Observatory\observatory-agent.exe" --uninstall
rem 或者直接远程卸载（不需要 UAC）。

setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title Observatory 受控端安装程序

set "CODE=@@ENROLL_CODE@@"
set "CODE_URL=@@ENROLL_CODE_URL@@"
set "VERSION=@@VERSION@@"
@@URL_LINES@@
set "TARGET=%TEMP%\observatory-install-%RANDOM%%RANDOM%.ps1"
set "GOT="

echo.
echo   ==================================================
echo      Observatory 受控端安装程序
echo      版本号 : %VERSION%
echo      目标机 : %COMPUTERNAME%
echo   ==================================================
echo.
echo   正在下载安装脚本（多个服务器地址会自动尝试）...

if defined URL1 call :Fetch "%URL1%"
if not defined GOT if defined URL2 call :Fetch "%URL2%"
if not defined GOT if defined URL3 call :Fetch "%URL3%"

if not defined GOT (
  echo.
  echo   失败：所有服务器地址都不可达。
  echo   请检查网络或代理后重新双击本文件。
  echo.
  pause
  exit /b 3
)

echo   下载完成，开始安装（只会弹一次 UAC 授权）...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%TARGET%" -Code "%CODE%"
set "RC=%ERRORLEVEL%"
del "%TARGET%" >nul 2>&1

if not "%RC%"=="0" (
  echo.
  echo   安装未完成，退出码 %RC%。
  echo   日志：%TEMP%\observatory-install.log
  echo.
  pause
  exit /b %RC%
)

echo.
echo   安装完成。之后的新版本会自动更新，不需要再运行本文件。
echo.
pause
exit /b 0

:Fetch
rem 依次尝试 curl（Win10 1803+ 自带）与 PowerShell 两种下载方式。
set "CAND=%~1"
set "PROBE=%TEMP%\observatory-install-probe-%RANDOM%.ps1"
del "%PROBE%" >nul 2>&1

curl.exe -fsSL --max-time 25 -o "%PROBE%" "%CAND%/api/v1/public/install.ps1?code=%CODE_URL%" >nul 2>&1
if not exist "%PROBE%" (
  powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 25 -Uri '%CAND%/api/v1/public/install.ps1?code=%CODE_URL%' -OutFile '%PROBE%' } catch { exit 1 }" >nul 2>&1
)

if exist "%PROBE%" (
  for %%A in ("%PROBE%") do (
    if %%~zA GTR 2000 (
      move /y "%PROBE%" "%TARGET%" >nul 2>&1
      if exist "%TARGET%" set "GOT=1"
    )
  )
)
del "%PROBE%" >nul 2>&1
exit /b 0