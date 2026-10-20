@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title Observatory 受控端安装

rem 离线/现场安装向导。
rem
rem 这里不内置任何注册码或域名：注册码一旦写死在 U 盘里就等于泄露，
rem 而域名写死会让双域名热备失效。

echo.
echo   ==================================================
echo      Observatory 受控端安装
echo   ==================================================
echo.

set "DEFAULT_URL=https://control.tianhw.top"
set "BASE_URL="
set /p BASE_URL=服务器地址 [%DEFAULT_URL%]:
if "%BASE_URL%"=="" set "BASE_URL=%DEFAULT_URL%"

set "ENROLL_CODE="
set /p ENROLL_CODE=注册码（在控制台「设置」页获取）:
if "%ENROLL_CODE%"=="" (
  echo.
  echo   缺少注册码，无法继续。
  pause
  exit /b 2
)

echo.
echo   正在从 %BASE_URL% 安装...
echo   安装过程只会弹一次 UAC，之后升级/卸载/控制都不会再弹。
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$u='%BASE_URL%/api/v1/public/install.ps1?code=%ENROLL_CODE%';" ^
  "try { irm $u -UseBasicParsing | iex } catch { Write-Host ('安装失败: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"

set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo   安装完成。
) else (
  echo   安装失败，退出码 %RC%。
  echo   可以检查 %TEMP%\observatory-install.log 或改用另一个域名重试。
)
echo.
pause
exit /b %RC%