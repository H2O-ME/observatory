@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title Observatory 受控端安装

rem 离线/现场安装向导（Observatory）。
rem
rem 这里不内置任何凭据：
rem   * 使用「部署准入密钥」（服务端 OBS_ENROLL_CODE）——控制台「设置 → 安装程序」
rem     查看/轮换；服务端未开启准入时可留空直接回车。
rem   * 密钥只作为命令行参数传给 install.ps1（-EnrollCode），**绝不**拼进脚本
rem     下载 URL——URL 会进反代访问日志；脚本本身是公开端点，也不能内嵌密钥。

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
set /p INSTALL_TOKEN=部署准入密钥（控制台「设置 → 安装程序」查看；未开启准入可直接回车）:

rem 修正路径反斜杠与引号，防止拼坏 PowerShell 字符串
set "PS_BASE_URL=%BASE_URL:'=''%"
set "PS_TOKEN=%INSTALL_TOKEN:'=''%"

echo.
echo   正在从 %BASE_URL% 安装...
echo   安装过程只会弹一次 UAC，之后升级/卸载/控制都不会再弹。
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "try {" ^
  "  $script = [scriptblock]::Create((irm '%PS_BASE_URL%/api/v1/public/install.ps1' -UseBasicParsing));" ^
  "  & $script -EnrollCode '%PS_TOKEN%';" ^
  "  exit $LASTEXITCODE;" ^
  "} catch { Write-Host ('安装失败: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"

set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo   安装完成。
) else (
  echo   安装失败，退出码 %RC%。
  echo   检查 %%TEMP%%\observatory-install.log；
  echo   若提示准入密钥无效，请在控制台「设置 → 安装程序」确认/轮换密钥后重试。
)
echo.
pause
exit /b %RC%
