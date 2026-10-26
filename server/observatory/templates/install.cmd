@echo off
rem Observatory agent installer -- double-click to install.
rem
rem This file carries a deployment ticket rendered by the server. It is
rem long-lived and reusable across machines by default, because it only gates
rem who may download and enrol the agent; revoke it in the console to kill it.
rem The legacy shared enrollment code was removed in 0.3.1.
rem
rem Rendered per request by the server:
rem   * the install token and version are already inside
rem   * several server addresses are embedded and tried in order
rem   * the real work happens in install.ps1, which asks for elevation once
rem
rem This file is intentionally plain ASCII: cmd.exe parses batch files using
rem the console code page, and non-ASCII text there breaks on some machines.
rem
rem Uninstall: "C:\Program Files\Observatory\observatory-agent.exe" --uninstall
rem or use the console; remote uninstall never prompts for UAC.

setlocal EnableExtensions
title Observatory agent installer

set "TOKEN=@@INSTALL_TOKEN@@"
set "TOKEN_URL=@@INSTALL_TOKEN_URL@@"
set "VERSION=@@VERSION@@"
@@URL_LINES@@
set "TARGET=%TEMP%\observatory-install-%RANDOM%%RANDOM%.ps1"
set "GOT="
set "PROBESIZE="

rem Renderers that left the @@...@@ markers in place did not inject a token.
rem A leftover marker is the only reliable signal of "not provided": tokens are
rem URL-safe and never contain '@'.
if not "%TOKEN%"=="%TOKEN:@=%" set "TOKEN="
if not "%TOKEN_URL%"=="%TOKEN_URL:@=%" set "TOKEN_URL="

set "CRED_Q="
if defined TOKEN (
  set "CRED_Q=token=%TOKEN_URL%"
)

if not defined CRED_Q (
  echo.
  echo   FAILED: this installer has no deployment ticket inside.
  echo   Download it again from the console: Settings - Installer.
  echo.
  pause
  exit /b 2
)

echo.
echo   ==================================================
echo      Observatory agent installer
echo      version : %VERSION%
echo      machine : %COMPUTERNAME%
echo      credential : %CRED%
echo   ==================================================
echo.
echo   Downloading the installer from the first reachable server...

if defined URL1 call :Fetch "%URL1%"
if not defined GOT if defined URL2 call :Fetch "%URL2%"
if not defined GOT if defined URL3 call :Fetch "%URL3%"

if not defined GOT (
  echo.
  echo   FAILED: none of the server addresses could be reached.
  echo   Check the network or proxy, then run this file again.
  echo.
  pause
  exit /b 3
)

echo   Download OK. Installing now ^(one UAC prompt^)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%TARGET%" -Token "%TOKEN%"
set "RC=%ERRORLEVEL%"
del "%TARGET%" >nul 2>&1

if not "%RC%"=="0" (
  echo.
  echo   Installation failed with exit code %RC%.
  echo   Log: %TEMP%\observatory-install.log
  echo.
  pause
  exit /b %RC%
)

echo.
echo   Done. Future versions update themselves; this file is not needed again.
echo.
pause
exit /b 0

:Fetch
rem Try curl.exe (Windows 10 1803+) first, then Windows PowerShell.
set "CAND=%~1"
set "PROBE=%TEMP%\observatory-probe-%RANDOM%.ps1"
del "%PROBE%" >nul 2>&1

curl.exe -fsSL --max-time 30 -o "%PROBE%" "%CAND%/api/v1/public/install.ps1?download=1&%CRED_Q%" >nul 2>&1
if not exist "%PROBE%" (
  powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri '%CAND%/api/v1/public/install.ps1?download=1&%CRED_Q%' -OutFile '%PROBE%' } catch { exit 1 }" >nul 2>&1
)

if not exist "%PROBE%" exit /b 0

for %%A in ("%PROBE%") do set "PROBESIZE=%%~zA"
if not defined PROBESIZE exit /b 0
if %PROBESIZE% GEQ 2000 (
  move /y "%PROBE%" "%TARGET%" >nul 2>&1
  if exist "%TARGET%" set "GOT=1"
)
del "%PROBE%" >nul 2>&1
exit /b 0