@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title Observatory 受控端安装

rem 离线/现场安装向导（Observatory 0.3.0）。
rem
rem 这里不内置任何凭据：
rem   * 0.3.0 推荐用「一次性安装令牌」——控制台生成，默认 30 分钟有效、
rem     单次使用、绑定首台设备；撤销方式见 README。
rem   * 旧版共享注册码仅用于兼容 0.2.x 受控端。
rem 令牌一旦写死在 U 盘里就失去“单次/可撤销”的意义，所以现场输入。

echo.
echo   ==================================================
echo      Observatory 受控端安装
echo   ==================================================
echo.

set "DEFAULT_URL=https://control.tianhw.top"
set "BASE_URL="
set /p BASE_URL=服务器地址 [%DEFAULT_URL%]:
if "%BASE_URL%"=="" set "BASE_URL=%DEFAULT_URL%"

set "INSTALL_TOKEN="
set /p INSTALL_TOKEN=一次性安装令牌（控制台生成；留空改用旧版注册码）:

set "ENROLL_CODE="
if not defined INSTALL_TOKEN (
  set /p ENROLL_CODE=旧版共享注册码:
  if not defined ENROLL_CODE (
    echo.
    echo   缺少安装凭据，无法继续。
    echo   请在控制台「设置 → 安装程序」生成一次性安装令牌。
    pause
    exit /b 2
  )
)

set "CRED_Q="
if defined INSTALL_TOKEN (
  set "CRED_Q=token=%INSTALL_TOKEN%"
) else (
  set "CRED_Q=code=%ENROLL_CODE%"
)

echo.
echo   正在从 %BASE_URL% 安装...
echo   安装过程只会弹一次 UAC，之后升级/卸载/控制都不会再弹。
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$u='%BASE_URL%/api/v1/public/install.ps1?%CRED_Q%';" ^
  "try { irm $u -UseBasicParsing | iex } catch { Write-Host ('安装失败: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"

set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo   安装完成。
) else (
  echo   安装失败，退出码 %RC%。
  echo   检查 %TEMP%\observatory-install.log，并确认令牌未过期/未被使用；
  echo   可在控制台重新生成令牌或用另一个域名重试。
)
echo.
pause
exit /b %RC%