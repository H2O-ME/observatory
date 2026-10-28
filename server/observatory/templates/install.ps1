# Observatory 受控端安装程序 / Observatory agent installer.
#
# 零凭据安装：脚本地址固定、下载与注册都不需要令牌。设备身份来自本机的
# MachineGuid，注册时服务端自动建立设备记录并下发设备令牌。
#
# 用法一（推荐，双击）：下载 Observatory-Install-<版本>.cmd，双击运行。
# 用法二（命令行）：
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://<控制台域名>/api/v1/public/install.ps1' | iex"
# 用法三（离线/U 盘）：把这个脚本与安装包放在同一目录，用 -Package 指定。

[CmdletBinding()]
param(
    [string[]]$BaseUrls = @(@@BASE_URLS@@),
    # 本地安装包（U 盘离线安装）：给了就完全不下载，直接用它。
    [string]$Package = '',
    [switch]$Uninstall,
    [switch]$Insecure,
    [switch]$Quiet,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ----------------------------------------------------------------- constants --
$ProductName    = 'Observatory'
$ServiceName    = 'ObservatoryAgent'
$InstallDir     = Join-Path $env:ProgramFiles 'Observatory'
$DataDir        = Join-Path $env:ProgramData 'Observatory'
$ExeName        = 'observatory-agent.exe'
$AgentVersion   = '@@VERSION@@'
$ConsoleHost    = '@@CONSOLE_HOST@@'
$RenderedAtMs   = '@@SERVER_TIME_MS@@'

try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

if ($Insecure) {
    # Development / self-signed deployments only.
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
}

$script:LogPath = Join-Path $env:TEMP 'observatory-install.log'

function Write-Step {
    param([string]$Message)
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $Message"
    if (-not $Quiet) { Write-Host $line -ForegroundColor Cyan }
    try { Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8 } catch { }
}

function Write-Fail {
    param([string]$Message)
    $line = "[$(Get-Date -Format 'HH:mm:ss')] 错误: $Message"
    Write-Host $line -ForegroundColor Red
    try { Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8 } catch { }
}

function Show-Banner {
    if ($Quiet) { return }
    Write-Host ''
    Write-Host '  ==================================================' -ForegroundColor DarkGray
    Write-Host '     Observatory 受控端安装程序' -ForegroundColor White
    Write-Host "     版本号 : $AgentVersion" -ForegroundColor Green
    Write-Host "     目标机 : $env:COMPUTERNAME" -ForegroundColor Gray
    Write-Host "     安装到 : $InstallDir" -ForegroundColor Gray
    Write-Host "     服务器 : $($BaseUrls -join '  |  ')" -ForegroundColor Gray
    Write-Host '  ==================================================' -ForegroundColor DarkGray
    Write-Host ''
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-BaseUrl {
    <# 返回第一个能成功访问 /api/v1/health 的地址；全部失败返回 $null。 #>
    if (-not $BaseUrls -or $BaseUrls.Count -eq 0) { return $null }
    foreach ($url in $BaseUrls) {
        $candidate = $url.TrimEnd('/')
        try {
            $health = Invoke-RestMethod -Uri "$candidate/api/v1/health" -TimeoutSec 8 -UseBasicParsing
            Write-Step "连接成功: $candidate (服务器版本 $($health.version))"
            return $candidate
        } catch {
            Write-Step "无法连接 $candidate : $($_.Exception.Message)"
        }
    }
    return $null
}

function Invoke-WithFailover {
    param(
        [string]$Path,
        [string]$OutFile = ''
    )
    foreach ($url in $BaseUrls) {
        $candidate = $url.TrimEnd('/')
        try {
            if ($OutFile) {
                Invoke-WebRequest -Uri "$candidate$Path" -OutFile $OutFile -TimeoutSec 300 -UseBasicParsing
            } else {
                return Invoke-RestMethod -Uri "$candidate$Path" -TimeoutSec 30 -UseBasicParsing
            }
            return $candidate
        } catch {
            Write-Step "请求失败 $candidate$Path : $($_.Exception.Message)"
        }
    }
    throw "所有服务器地址都不可达: $Path"
}

function Request-Elevation {
    <# 唯一的 UAC 提示发生在这里。 #>
    # 本地脚本（U 盘离线安装）直接原地提权重跑：不联网、不重新下载脚本，
    # 也把 -Package 一起带过去。只有 `irm | iex` 这种没有脚本路径的运行方式
    # 才需要回服务器取一份脚本。
    if ($PSCommandPath -and (Test-Path -LiteralPath $PSCommandPath)) {
        $arguments = @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', ("`"{0}`"" -f $PSCommandPath)
        )
        if ($Package) {
            # 提权子进程的工作目录会变，先把本地包路径钉成绝对路径。
            $packageArg = $Package
            if (-not [System.IO.Path]::IsPathRooted($packageArg)) {
                $packageArg = Join-Path (Get-Location).Path $packageArg
            }
            $arguments += @('-Package', ("`"{0}`"" -f $packageArg))
        }
        if ($Insecure) { $arguments += '-Insecure' }
        if ($Quiet) { $arguments += '-Quiet' }
        if ($NoStart) { $arguments += '-NoStart' }
        Write-Step '需要一次管理员授权来安装服务（整个生命周期中只有这一次）'
        try {
            $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -Wait -PassThru
            exit $process.ExitCode
        } catch {
            Write-Fail '提权被取消，安装中止'
            exit 4
        }
    }

    $primary = Resolve-BaseUrl
    if (-not $primary) {
        Write-Fail '两个域名都不可达，无法继续安装。请检查网络或代理。'
        exit 3
    }
    # 提权子进程从服务器重新下载脚本（脚本本身不含任何凭据）。
    $childUrl = "$primary/api/v1/public/install.ps1"
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-Command',
        "`$ProgressPreference='SilentlyContinue'; iwr '$childUrl' -UseBasicParsing | iex"
    )
    if ($Insecure) {
        $arguments = @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-Command',
            "`$ProgressPreference='SilentlyContinue'; [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { `$true }; iwr '$childUrl' -UseBasicParsing | iex"
        )
    }
    Write-Step '需要一次管理员授权来安装服务（整个生命周期中只有这一次）'
    try {
        $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $process.ExitCode
    } catch {
        Write-Fail '提权被取消，安装中止'
        exit 4
    }
}

function Stop-Existing {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($service) {
        if ($service.Status -ne 'Stopped') {
            Write-Step '停止已有服务'
            try { Stop-Service -Name $ServiceName -Force -ErrorAction Stop } catch { }
            for ($i = 0; $i -lt 40; $i++) {
                Start-Sleep -Milliseconds 500
                $service.Refresh()
                if ($service.Status -eq 'Stopped') { break }
            }
        }
        Write-Step '移除旧的服务注册'
        & sc.exe delete $ServiceName | Out-Null
    }
    Get-Process -Name 'observatory-agent' -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Step "结束残留进程 pid=$($_.Id)"
        try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch { }
    }
    # 旧版本更新器可能仍在等待，一并清理。
    Get-Process -Name 'observatory-updater' -ErrorAction SilentlyContinue | ForEach-Object {
        try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch { }
    }
    Start-Sleep -Milliseconds 700
}

function Install-Files {
    param([string]$PackagePath)
    $staging = Join-Path $env:TEMP "observatory-$([guid]::NewGuid().ToString('N').Substring(0,8))"
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    try {
        Write-Step '解压安装包'
        Expand-Archive -LiteralPath $PackagePath -DestinationPath $staging -Force

        if (-not (Test-Path (Join-Path $staging $ExeName))) {
            $nested = Get-ChildItem -Path $staging -Recurse -Filter $ExeName | Select-Object -First 1
            if ($nested) { $staging = $nested.DirectoryName } else { throw "安装包里找不到 $ExeName" }
        }

        if (Test-Path $InstallDir) {
            Write-Step "清理旧安装目录 $InstallDir"
            Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
        Copy-Item -Path (Join-Path $staging '*') -Destination $InstallDir -Recurse -Force
        # zip 内已有 build.ps1 写入的正确 VERSION（受控端 --version 优先读它），
        # 不再用服务器版本覆盖，否则 agent 自报版本与二进制不一致（0.3.1 修复）。
        Write-Step "已安装到 $InstallDir"
    }
    finally {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Install-Service {
    $exe = Join-Path $InstallDir $ExeName
    if (-not (Test-Path $exe)) { throw "找不到 $exe" }

    Write-Step '注册并启动服务'
    # 注册不需要任何凭据：受控端自己用 MachineGuid 生成设备标识，服务端自动建记录。
    $arguments = @('--install', '--exe', "`"$exe`"")
    foreach ($url in $BaseUrls) { $arguments += @('--base-url', $url) }

    $output = & $exe @arguments 2>&1
    if (-not $Quiet) { $output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }

    if (-not $NoStart) {
        try {
            Start-Service -Name $ServiceName -ErrorAction Stop
        } catch {
            & $exe --start | Out-Null
        }
        Start-Sleep -Seconds 2
        $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if (-not $service) { throw '服务未注册成功' }
        if ($service.Status -ne 'Running') {
            Write-Step "服务状态: $($service.Status)（稍后会自动重试启动）"
        } else {
            Write-Step "服务已启动: $ServiceName"
        }
    }
}

function Uninstall-Agent {
    $exe = Join-Path $InstallDir $ExeName
    if (Test-Path $exe) {
        Write-Step '调用受控端卸载程序'
        & $exe --uninstall | Out-Null
        Start-Sleep -Seconds 4
    }
    Stop-Existing
    foreach ($path in @($InstallDir, $DataDir)) {
        if (Test-Path $path) {
            Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
            Write-Step "已删除 $path"
        }
    }
    Write-Host ''
    Write-Host "  Observatory 受控端 $AgentVersion 已卸载" -ForegroundColor Green
    Write-Host ''
}

# -------------------------------------------------------------------- main --
Show-Banner

if (-not (Test-Admin)) {
    if ($Uninstall) {
        Write-Step '卸载需要管理员权限，正在请求（仅此一次）'
    }
    Request-Elevation
    return
}

if ($Uninstall) {
    Write-Step '开始卸载'
    Uninstall-Agent
    exit 0
}

if (-not $BaseUrls -or $BaseUrls.Count -eq 0) {
    Write-Fail '缺少服务器地址。'
    exit 2
}

try {
    $primary = Resolve-BaseUrl
    if (-not $primary) { throw '两个域名都不可达' }

    $downloaded = $false
    $localPackage = ''
    if ($Package) {
        # 相对路径按"当前工作目录"解析后转成绝对路径：提权子进程会换工作目录。
        $candidate = $Package
        if (-not [System.IO.Path]::IsPathRooted($candidate)) {
            $candidate = Join-Path (Get-Location).Path $candidate
        }
        if (-not (Test-Path -LiteralPath $candidate)) { throw "找不到本地安装包: $Package" }
        $localPackage = (Resolve-Path -LiteralPath $candidate).Path
    }

    if ($localPackage) {
        $packagePath = $localPackage
        $targetVersion = $AgentVersion
        $matched = [regex]::Match((Split-Path -Leaf $localPackage), '(\d+\.\d+\.\d+)')
        if ($matched.Success) { $targetVersion = $matched.Groups[1].Value }
        $shaFile = "$localPackage.sha256"
        if (Test-Path -LiteralPath $shaFile) {
            $expectedSha = ((Get-Content -LiteralPath $shaFile -Raw) -split '\s+')[0].Trim()
            $actual = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLower()
            if ($actual -ne $expectedSha.ToLower()) {
                throw "本地安装包校验失败 (期望 $expectedSha, 实际 $actual)"
            }
            Write-Step "本地安装包校验通过 sha256=$($actual.Substring(0,16))..."
        } else {
            Write-Step '使用本地安装包（同目录没有 .sha256，跳过校验）'
        }
        Write-Step "本地安装包: $packagePath (版本 $targetVersion)"
    } else {
        Write-Step '查询服务器上的受控端版本'
        $package = Invoke-WithFailover -Path '/api/v1/public/agent/latest'
        $targetVersion = $AgentVersion
        $expectedSha = ''
        if ($package -and $package.available) {
            $targetVersion = $package.version
            $expectedSha = [string]$package.sha256
            Write-Step "服务器提供版本: $targetVersion"
        } else {
            Write-Step "服务器没有上传安装包，使用脚本内置版本 $AgentVersion"
        }

        $packagePath = Join-Path $env:TEMP "observatory-agent-$targetVersion.zip"
        Write-Step "下载安装包 (版本 $targetVersion)"
        Invoke-WithFailover -Path "/api/v1/public/agent/$targetVersion/package" -OutFile $packagePath | Out-Null
        $downloaded = $true

        if ($expectedSha) {
            $actual = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLower()
            if ($actual -ne $expectedSha.ToLower()) {
                Remove-Item -LiteralPath $packagePath -Force -ErrorAction SilentlyContinue
                throw "安装包校验失败 (期望 $expectedSha, 实际 $actual)"
            }
            Write-Step "校验通过 sha256=$($actual.Substring(0,16))..."
        }
    }

    Stop-Existing
    $AgentVersion = $targetVersion
    Install-Files -PackagePath $packagePath
    Install-Service
    # 下载到临时目录的那份才删；U 盘上的本地包留给下一台机器。
    if ($downloaded) {
        Remove-Item -LiteralPath $packagePath -Force -ErrorAction SilentlyContinue
    }

    $deviceUid = ''
    $configPath = Join-Path $DataDir 'config.json'
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Milliseconds 600
        if (Test-Path $configPath) {
            try {
                $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
                if ($config.device_uid) { $deviceUid = $config.device_uid; break }
            } catch { }
        }
    }

    if (-not $Quiet) {
        Write-Host ''
        Write-Host '  ==================================================' -ForegroundColor DarkGray
        Write-Host '     安装完成' -ForegroundColor Green
        Write-Host "     版本号   : $targetVersion" -ForegroundColor Green
        Write-Host "     设备标识 : $deviceUid" -ForegroundColor Gray
        Write-Host "     服务名   : $ServiceName" -ForegroundColor Gray
        Write-Host "     控制台   : https://$ConsoleHost" -ForegroundColor Gray
        Write-Host '  ==================================================' -ForegroundColor DarkGray
        Write-Host ''
        Write-Host '  以后无需重新安装：服务器上的新版本会自动推送到本机。' -ForegroundColor DarkGray
        Write-Host '  远程卸载与远程控制均不会弹出 UAC。' -ForegroundColor DarkGray
        Write-Host ''
    }
    exit 0
}
catch {
    Write-Fail $_.Exception.Message
    Write-Host "  详细日志: $script:LogPath" -ForegroundColor DarkGray
    exit 1
}
