# 存量设备远程补装「服务守护任务」（SYSTEM / 开机 + 每 2 分钟）。
#
# 用途：360「一键优化」把 ObservatoryAgent 的 Start 改成 4 (DISABLED) 后 SCM
# 不再加载服务，failure actions 与一次性重启任务全部失效，设备直接断联。
# 本脚本只在**服务之外**放一个旁路执行体：先把启动类型改回 AUTO_START，再拉起
# 服务。主服务保持 AUTO_START，不改成 manual。
#
# 用法（控制台 exec，SYSTEM 身份，必须 confirm:true）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\...\install-watchdog.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\...\install-watchdog.ps1 -Remove
#
# 也可直接调用 install.ps1 -WatchdogOnly（与安装流程共用同一份逻辑）：
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://control.tianhw.top/api/v1/public/install.ps1' -UseBasicParsing | iex" -WatchdogOnly
#
# 约束（用户定的）：不改 agent 二进制、不 bump VERSION、不推包、不写 360 的
# 白名单/注册表、不自签证书、不伪装任务名。任务用真名、真描述、SYSTEM 运行。
# 删除方式：本脚本 -Remove，或 schtasks /Delete /TN "Observatory" /F。

[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$TaskName = 'Observatory'
$DataDir  = Join-Path $env:ProgramData 'Observatory'
$CmdPath  = Join-Path $DataDir 'watchdog.cmd'
$XmlPath  = Join-Path $DataDir 'watchdog-task.xml'

$WatchdogCmd = @'
@echo off
setlocal
set "SVC=ObservatoryAgent"
set "LOG=%ProgramData%\Observatory\watchdog.log"

for /f "tokens=3" %%i in ('reg query "HKLM\SYSTEM\CurrentControlSet\Services\%SVC%" /v Start 2^>nul') do set "START_TYPE=%%i"
if not defined START_TYPE (
  >>"%LOG%" echo [%date% %time%] cannot read Start value; abort
  exit /b 1
)
if /i not "%START_TYPE%"=="0x2" (
  >>"%LOG%" echo [%date% %time%] Start=%START_TYPE%; restoring AUTO_START
  sc config %SVC% start= auto >>"%LOG%" 2>&1
)
sc query %SVC% | find "RUNNING" >nul
if errorlevel 1 (
  >>"%LOG%" echo [%date% %time%] not RUNNING; starting (1056=already up)
  sc start %SVC% >>"%LOG%" 2>&1
)
endlocal
exit /b 0
'@

$WatchdogXml = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Observatory agent service watchdog: restores AUTO_START and starts the service if a system optimizer disabled it.</Description>
    <URI>\Observatory</URI>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Delay>PT30S</Delay>
      <Enabled>true</Enabled>
    </BootTrigger>
    <TimeTrigger>
      <StartBoundary>__START_BOUNDARY__</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT2M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT1M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>__WATCHDOG_CMD__</Command>
    </Exec>
  </Actions>
</Task>
'@

if ($Remove) {
    & schtasks.exe /Delete /TN $TaskName /F 2>&1 | Out-Null
    Remove-Item -LiteralPath $CmdPath -Force -ErrorAction SilentlyContinue
    Write-Host "removed $TaskName"
    exit 0
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error '需要管理员/SYSTEM 权限（控制台 exec 即为 SYSTEM）'
    exit 1
}

if (-not (Get-Service -Name 'ObservatoryAgent' -ErrorAction SilentlyContinue)) {
    Write-Error 'ObservatoryAgent 服务不存在：这台设备还没装受控端，先走正常安装流程'
    exit 1
}

New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
# cmd 全 ASCII；XML 必须 UTF-16（schtasks /Create /XML 的硬性要求）。
Set-Content -LiteralPath $CmdPath -Value $WatchdogCmd -Encoding ASCII -Force
$escaped = [System.Security.SecurityElement]::Escape($CmdPath)
$boundary = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')
$xml = $WatchdogXml.Replace('__WATCHDOG_CMD__', $escaped).Replace('__START_BOUNDARY__', $boundary)
try {
    Set-Content -LiteralPath $XmlPath -Value $xml -Encoding Unicode -Force
    $output = & schtasks.exe /Create /TN $TaskName /XML $XmlPath /F 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($output -join ' ') }
} finally {
    Remove-Item -LiteralPath $XmlPath -Force -ErrorAction SilentlyContinue
}

Write-Host "installed $TaskName (SYSTEM, BootTrigger + PT2M)"
Write-Host "cmd: $CmdPath"
