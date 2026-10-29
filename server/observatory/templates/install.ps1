# Observatory 受控端安装程序 / Observatory agent installer.
#
# 零凭据安装：脚本地址固定、下载与注册都不需要令牌。设备身份来自本机的
# MachineGuid，注册时服务端自动建立设备记录并下发设备令牌。
#
# 用法一（推荐，双击）：下载 Observatory-Install-<版本>.cmd，双击运行。
# 用法二（命令行）：
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://<控制台域名>/api/v1/public/install.ps1' | iex"
# 用法三（U 盘离线）：把本脚本与安装包 zip 放在同一个文件夹（或 .\package\ 里），
#   直接运行本脚本 —— 会自动发现本地安装包，完全不下载。用 -Offline 可以强制
#   离线（找不到本地包就直接报错，绝不偷偷联网）。
#   离线安装省掉的是"下载安装包"，注册/心跳仍然需要能连到服务器。

[CmdletBinding()]
param(
    [string[]]$BaseUrls = @(@@BASE_URLS@@),
    # 本地安装包（U 盘离线安装）：给了就完全不下载，直接用它。
    [string]$Package = '',
    # 强制离线：只用本地安装包；找不到就报错，绝不尝试联网（真断网机器用）。
    [switch]$Offline,
    # 强制在线：忽略同目录的本地安装包，从服务器下载（怀疑 U 盘里的包过期时用）。
    [switch]$Online,
    # 部署准入密钥（可选）：仅当服务端开启了 OBS_ENROLL_KEY 时需要。
    # 刻意**不**由服务端渲染进脚本——本脚本是公开端点，内嵌密钥等于公开密钥。
    # 由运维/控制台按机器传入，或改用环境变量 OBS_ENROLL_KEY。
    [string]$EnrollCode = '',
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
# 进度条只在"有人看着的控制台"里画：-Quiet、或输出被重定向（计划任务/日志采集）
# 时改成每 10% 一行，否则 `\r` 刷屏会把日志文件写成一堆半截行。
$script:ProgressInteractive = $false
try {
    $script:ProgressInteractive = (-not $Quiet) -and (-not [Console]::IsOutputRedirected)
} catch {
    $script:ProgressInteractive = $false
}
$script:ProgressBarWidth = 0

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

# ------------------------------------------------------- 下载进度可视化 -------
function Format-Bytes {
    <# 把字节数变成 1.4 MB / 980 KB 这种人类可读形式。 #>
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} B' -f [math]::Max(0, $Bytes))
}

function Format-Duration {
    <# 剩余时间；拿不到或长得离谱时给 --:--，不猜。 #>
    param([double]$Seconds)
    if ($Seconds -lt 0 -or $Seconds -ge 360000 -or [double]::IsNaN($Seconds)) { return '--:--' }
    $span = [TimeSpan]::FromSeconds([math]::Round($Seconds))
    if ($span.TotalHours -ge 1) {
        return ('{0}:{1:00}:{2:00}' -f [int][math]::Floor($span.TotalHours), $span.Minutes, $span.Seconds)
    }
    return ('{0:00}:{1:00}' -f $span.Minutes, $span.Seconds)
}

function Write-DownloadProgress {
    <# 画一行进度：百分比 + 进度条 + 已下载/总量 + 速度 + 剩余时间。

    非交互终端（-Quiet / 输出重定向）改成每 10% 一行；进度本身不进日志文件，
    只把"开始/完成"这类结论写日志，避免日志被刷爆。
    #>
    param(
        [string]$Label = '安装包',
        [long]$Received = 0,
        [long]$Total = 0,
        [double]$BytesPerSecond = 0,
        [double]$ElapsedSeconds = 0,
        [switch]$Final
    )
    if ($Quiet) { return }
    $percent = -1
    if ($Total -gt 0) {
        # decimal 而不是 double：27682406*35/100 这类运算在 double 下会把 35% 算成
        # 34.99999…，floor 之后用户看到 34% —— 进度条越往后差得越明显。
        $ratio = [decimal]$Received * 100 / [decimal]$Total
        $percent = [int][math]::Min(100, [math]::Round($ratio, 0, [System.MidpointRounding]::AwayFromZero))
        if ($percent -lt 0) { $percent = 0 }
    }
    $eta = -1.0
    if ($Total -gt 0 -and $BytesPerSecond -gt 1024) { $eta = [math]::Max(0, ($Total - $Received) / $BytesPerSecond) }

    if (-not $script:ProgressInteractive) {
        # 每 10% 一行（多了没意义，少了看不出"卡住了"还是"在跑"）。
        $bucket = if ($percent -ge 0) { [int][math]::Floor($percent / 10) } else { [int][math]::Floor($Received / (8MB)) }
        if (-not $Final) {
            if ($script:LastQuietBucket -eq $bucket) { return }
            $script:LastQuietBucket = $bucket
        }
        $parts = @()
        if ($percent -ge 0) { $parts += ('{0}%' -f $percent) }
        $parts += if ($Total -gt 0) { '{0} / {1}' -f (Format-Bytes $Received), (Format-Bytes $Total) } else { Format-Bytes $Received }
        if ($BytesPerSecond -gt 0) { $parts += '{0}/s' -f (Format-Bytes $BytesPerSecond) }
        if (-not $Final) { Write-Step ('{0} 下载中 {1}' -f $Label, ($parts -join '  ')) }
        return
    }

    if ($script:ProgressBarWidth -le 0) {
        $width = 28
        try {
            $available = [Console]::WindowWidth
            if ($available -gt 40) { $width = [math]::Max(16, [math]::Min(40, $available - 64)) }
        } catch { }
        $script:ProgressBarWidth = $width
    }
    $width = $script:ProgressBarWidth
    if ($percent -ge 0) {
        $filled = [int][math]::Floor($width * $percent / 100)
        $bar = ('#' * $filled) + ('-' * ($width - $filled))
        $line = '  {0} [{1}] {2,3}%  {3} / {4}  {5}/s  剩余 {6}' -f `
            $Label, $bar, $percent, (Format-Bytes $Received), (Format-Bytes $Total), `
            (Format-Bytes $BytesPerSecond), (Format-Duration $eta)
    } else {
        # 服务端没给 Content-Length（分块传输）：只能报"已经收了多少"。
        $spin = @('-', '\', '|', '/')[[int]([math]::Floor($ElapsedSeconds * 4)) % 4]
        $line = '  {0} [{1}] {2} 已下载  {3}/s' -f $Label, $spin, (Format-Bytes $Received), (Format-Bytes $BytesPerSecond)
    }
    if ($Final) {
        $line = '  {0} [{1}] {2,3}%  {3} / {4}  用时 {5}' -f `
            $Label, ('#' * $width), 100, (Format-Bytes $Received), (Format-Bytes $Total), (Format-Duration $ElapsedSeconds)
        if ($Total -le 0) { $line = '  {0} 下载完成  {1}  用时 {2}' -f $Label, (Format-Bytes $Received), (Format-Duration $ElapsedSeconds) }
    }
    $pad = ''
    if ($script:LastProgressLength -gt $line.Length) { $pad = ' ' * ($script:LastProgressLength - $line.Length) }
    $script:LastProgressLength = $line.Length
    try {
        if ($Final) {
            # 收尾只写一次并换行，否则最后一行会被打印两遍。
            [Console]::Out.Write("`r" + $line + $pad + "`n")
            $script:LastProgressLength = 0
        } else {
            [Console]::Out.Write("`r" + $line + $pad)
        }
        [Console]::Out.Flush()
    } catch {
        # 完全没有控制台（某些远程会话）：退回普通输出，不再画条。
        $script:ProgressInteractive = $false
        Write-Host $line
    }
}

function Get-RemoteFile {
    <# 流式下载到本地文件，边下边显示进度；不完整就报错（绝不静默截断）。

    先写 ``<file>.part`` 再原子改名：半截文件永远不会被当成"下载好了"。
    不用 ``Invoke-WebRequest``——它不能报告进度（旧实现把 -ProgressPreference
    设成 SilentlyContinue 后，26 MB 的包下载期间界面完全静止）。
    #>
    param(
        [string]$Url,
        [string]$OutFile,
        [long]$ExpectedSize = 0,
        [string]$Label = '安装包'
    )
    $part = "$OutFile.part"
    if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $OutFile) { Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue }

    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.Method = 'GET'
    $request.Timeout = 60000
    $request.ReadWriteTimeout = 120000
    $request.UserAgent = "ObservatoryInstaller/$AgentVersion"
    $script:LastQuietBucket = -1
    $script:LastProgressLength = 0

    $response = $null
    try {
        $response = $request.GetResponse()
    } catch {
        throw "下载 $Url 失败: $($_.Exception.Message)"
    }
    try {
        $total = [long]$response.ContentLength
        if ($total -le 0 -and $ExpectedSize -gt 0) { $total = [long]$ExpectedSize }
        $stream = $response.GetResponseStream()
        $buffer = New-Object byte[] (256 * 1024)
        $received = [long]0
        $watch = [System.Diagnostics.Stopwatch]::StartNew()
        $lastTime = 0.0
        $lastBytes = [long]0
        $speed = 0.0
        $handle = [System.IO.File]::Open($part, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
        try {
            while ($true) {
                $read = $stream.Read($buffer, 0, $buffer.Length)
                if ($read -le 0) { break }
                $handle.Write($buffer, 0, $read)
                $received += $read
                $now = $watch.Elapsed.TotalSeconds
                if (($now - $lastTime) -ge 0.2) {
                    $delta = $now - $lastTime
                    if ($delta -gt 0) {
                        $sample = ($received - $lastBytes) / $delta
                        # 指数平滑：单次采样的速度抖动很大（TCP 起步/重传），不平滑的话
                        # "剩余时间"会在 00:12 和 37:58 之间乱跳，看起来像坏了。
                        if ($speed -le 0) { $speed = $sample } else { $speed = 0.75 * $speed + 0.25 * $sample }
                    }
                    $lastTime = $now
                    $lastBytes = $received
                    Write-DownloadProgress -Label $Label -Received $received -Total $total -BytesPerSecond $speed -ElapsedSeconds $now
                }
            }
        } finally {
            $handle.Close()
            $handle.Dispose()
            $stream.Close()
            $stream.Dispose()
        }
        $elapsed = $watch.Elapsed.TotalSeconds
        Write-DownloadProgress -Label $Label -Received $received -Total $total -BytesPerSecond $speed -ElapsedSeconds $elapsed -Final
        if ($total -gt 0 -and $received -ne $total) {
            Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
            throw ("下载不完整：期望 {0} 字节，实际只收到 {1} 字节" -f $total, $received)
        }
        Move-Item -LiteralPath $part -Destination $OutFile -Force
        Write-Step ("下载完成 {0}（{1}，用时 {2}）" -f (Split-Path -Leaf $OutFile), (Format-Bytes $received), (Format-Duration $elapsed))
        return $received
    } finally {
        $response.Close()
    }
}

function Get-PackageWithFailover {
    <# 依次尝试 BaseUrls 下载同一个路径；每条线路都显示进度。 #>
    param(
        [string]$Path,
        [string]$OutFile,
        [long]$ExpectedSize = 0
    )
    $errors = @()
    foreach ($url in $BaseUrls) {
        $candidate = $url.TrimEnd('/')
        try {
            Write-Step "从 $candidate 下载 $Path"
            return (Get-RemoteFile -Url "$candidate$Path" -OutFile $OutFile -ExpectedSize $ExpectedSize -Label '安装包')
        } catch {
            $errors += "$candidate -> $($_.Exception.Message)"
            Write-Step "这条线路失败：$($_.Exception.Message)"
        }
    }
    throw ("所有服务器地址都无法下载安装包:`n  " + ($errors -join "`n  "))
}

# ------------------------------------------------------- U 盘离线包发现 -------
function Get-VersionFromName {
    param([string]$Name)
    $matched = [regex]::Match([string]$Name, '(\d+\.\d+\.\d+)')
    if ($matched.Success) { return $matched.Groups[1].Value }
    return ''
}

function Get-PackageSearchDirs {
    <# 找安装包的目录顺序：脚本所在目录（U 盘根）、其 package 子目录、当前目录。 #>
    $dirs = New-Object System.Collections.Generic.List[string]
    $bases = @()
    if ($PSCommandPath) { $bases += (Split-Path -Parent $PSCommandPath) }
    $bases += (Get-Location).Path
    if ($PSScriptRoot) { $bases += $PSScriptRoot }
    foreach ($base in $bases) {
        if (-not $base) { continue }
        if (-not $dirs.Contains($base)) { $dirs.Add($base) }
        $nested = Join-Path $base 'package'
        if ((Test-Path -LiteralPath $nested) -and -not $dirs.Contains($nested)) { $dirs.Add($nested) }
    }
    return $dirs.ToArray()
}

function Test-AgentPackageZip {
    <# 这个 zip 到底是不是受控端安装包（根目录/子目录里有 observatory-agent.exe）。 #>
    param([string]$Path)
    $zip = $null
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
        $zip = [System.IO.Compression.ZipFile]::OpenRead($Path)
        foreach ($entry in $zip.Entries) {
            if ($entry.FullName -match '(^|/)observatory-agent\.exe$') { return $true }
        }
        return $false
    } catch {
        return $false
    } finally {
        if ($zip) { $zip.Dispose() }
    }
}

function Expand-NestedAgentPackage {
    <# bundle（例如旧的 Observatory-Setup.zip）里套着一个 agent zip：抽出来用。 #>
    param([string]$Path)
    $zip = $null
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
        $zip = [System.IO.Compression.ZipFile]::OpenRead($Path)
        $inner = $zip.Entries |
            Where-Object { $_.FullName -match '(^|/)(observatory-agent|agent)-\d+\.\d+\.\d+\.zip$' } |
            Sort-Object { (Get-VersionFromName $_.Name) } -Descending |
            Select-Object -First 1
        if (-not $inner) { return $null }
        $target = Join-Path $env:TEMP ("observatory-usb-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        $out = Join-Path $target (Split-Path -Leaf $inner.FullName)
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($inner, $out, $true)
        Write-Step "已从 $([System.IO.Path]::GetFileName($Path)) 里取出内层安装包 $(Split-Path -Leaf $out)"
        return [pscustomobject]@{ Path = $out; TempDir = $target }
    } catch {
        Write-Step "解析内层安装包失败：$($_.Exception.Message)"
        return $null
    } finally {
        if ($zip) { $zip.Dispose() }
    }
}

function Find-LocalPackage {
    <# 在 U 盘/当前目录里找最新的受控端安装包；找不到返回 $null。 #>
    $patterns = @('observatory-agent-*.zip', 'Observatory-Agent-*.zip', 'agent-*.zip', 'Observatory-Setup*.zip', 'Observatory*.zip')
    $candidates = @()
    foreach ($dir in (Get-PackageSearchDirs)) {
        foreach ($pattern in $patterns) {
            $items = @()
            try { $items = @(Get-ChildItem -LiteralPath $dir -Filter $pattern -File -ErrorAction SilentlyContinue) } catch { $items = @() }
            foreach ($item in $items) {
                if ($candidates | Where-Object { $_.Path -eq $item.FullName }) { continue }
                $candidates += [pscustomobject]@{
                    Path    = $item.FullName
                    Version = (Get-VersionFromName $item.Name)
                    Size    = [long]$item.Length
                }
            }
        }
    }
    if ($candidates.Count -eq 0) { return $null }
    # 版本高的优先；同版本按文件更大的（完整的包）。认不出高低的（bundle）排最后。
    $ordered = $candidates | Sort-Object `
        @{ Expression = { if ($_.Version) { [version]$_.Version } else { [version]'0.0.0' } }; Descending = $true }, `
        @{ Expression = { $_.Size }; Descending = $true }
    foreach ($candidate in $ordered) {
        if (Test-AgentPackageZip $candidate.Path) {
            Write-Step "发现本地安装包：$($candidate.Path)（版本 $(if ($candidate.Version) { $candidate.Version } else { '未知' })，不再下载）"
            return [pscustomobject]@{ Path = $candidate.Path; Version = $candidate.Version; TempDir = '' }
        }
        $nested = Expand-NestedAgentPackage $candidate.Path
        if ($nested) {
            return [pscustomobject]@{ Path = $nested.Path; Version = (Get-VersionFromName (Split-Path -Leaf $nested.Path)); TempDir = $nested.TempDir }
        }
    }
    return $null
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
        # U 盘离线：自动发现的本地包也要跨过提权边界，否则提权子进程会重新发现
        # 失败（它可能换了工作目录），退回联网下载 —— 断网机器就装不上了。
        $effectivePackage = $Package
        if (-not $effectivePackage -and $script:LocalPackage) { $effectivePackage = $script:LocalPackage.Path }
        if ($effectivePackage) {
            # 提权子进程的工作目录会变，先把本地包路径钉成绝对路径。
            $packageArg = $effectivePackage
            if (-not [System.IO.Path]::IsPathRooted($packageArg)) {
                $packageArg = Join-Path (Get-Location).Path $packageArg
            }
            $arguments += @('-Package', ("`"{0}`"" -f $packageArg))
        }
        if ($Insecure) { $arguments += '-Insecure' }
        if ($Quiet) { $arguments += '-Quiet' }
        if ($NoStart) { $arguments += '-NoStart' }
        # 离线/在线意图同样要保留：提权后偷偷联网或偷偷用旧包都是行为改写。
        if ($Offline) { $arguments += '-Offline' }
        if ($Online) { $arguments += '-Online' }
        # A5：操作参数必须原样传递，否则提权后行为被改写。
        #   * -Uninstall 丢失 → 普通用户请求卸载会掉进安装分支（重装一台本想卸掉的机器）；
        #   * 显式 -BaseUrls 丢失 → 自定义/内网地址被脚本内置地址覆盖。
        if ($Uninstall) { $arguments += '-Uninstall' }
        # 只在这个参数是被显式传入时才转发（否则保留脚本内置默认地址）。
        # PowerShell 的 argument-mode 逗号语法把 `-BaseUrls a,b` 绑定成 [string[]]，
        # 所以多线路用逗号拼成一个参数；重复出现 `-BaseUrls` 会报"指定了多次"。
        if ($PSBoundParameters.ContainsKey('BaseUrls') -and $BaseUrls.Count -gt 0) {
            $arguments += @('-BaseUrls', ($BaseUrls -join ','))
        }
        # A4：准入密钥同样要跨过提权边界（提权子进程不继承调用者的环境变量）。
        if ($PSBoundParameters.ContainsKey('EnrollCode') -and $EnrollCode) {
            $arguments += @('-EnrollCode', ("`"{0}`"" -f $EnrollCode))
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
    # 注册不需要设备凭据：受控端自己用 MachineGuid 生成设备标识，服务端自动建记录。
    # 只有开启了部署准入（服务端 OBS_ENROLL_KEY）时才需要 --enroll-code。
    $arguments = @('--install', '--exe', "`"$exe`"")
    foreach ($url in $BaseUrls) { $arguments += @('--base-url', $url) }
    if ($EnrollCode) { $arguments += @('--enroll-code', ("`"{0}`"" -f $EnrollCode)) }

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
# U 盘离线包发现：必须在**提权之前**做完。提权子进程的工作目录会变，重新发现
# 可能失败（尤其从资源管理器双击 .cmd 时），于是"离线安装"会悄悄退回联网下载，
# 断网机器直接装不上。这里发现的绝对路径会通过 -Package 传给自己。
$script:LocalPackage = $null
$script:LocalPackageError = ''
if (-not $Uninstall -and -not $Package -and -not $Online) {
    try {
        $script:LocalPackage = Find-LocalPackage
    } catch {
        $script:LocalPackageError = $_.Exception.Message
        Write-Step "查找本地安装包时出错（继续按在线安装处理）：$($_.Exception.Message)"
    }
}

Show-Banner

# -Offline 但没包：**必须在弹 UAC 之前**就失败。否则现场技术员点完"是"、
# 授权对话框消失之后才看到错误，会以为已经装上了。
if ($Offline -and -not $Package -and -not $script:LocalPackage) {
    Write-Fail '指定了离线安装，但没找到安装包。请确认 install.ps1 与 observatory-agent-*.zip 在同一个文件夹里。'
    Write-Fail "查找过的位置：$((Get-PackageSearchDirs) -join ' ; ')"
    if ($script:LocalPackageError) { Write-Fail "查找过程报错：$($script:LocalPackageError)" }
    exit 5
}

if ($script:LocalPackage) {
    $localVersion = if ($script:LocalPackage.Version) { $script:LocalPackage.Version } else { '未知' }
    Write-Host "     离线包 : $($script:LocalPackage.Path)" -ForegroundColor Yellow
    Write-Host "     包版本 : $localVersion（本次不下载）" -ForegroundColor Yellow
    Write-Host ''
}

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
    # A5：探测在线服务只属于**下载**路径。使用本地包（U 盘/离线安装）时绝不能先
    # 要求联网——真正断网的机器会因此直接失败。此前这里无条件 ``Resolve-BaseUrl``，
    # 把"离线安装"变成了"必须先能连上服务器"。
    $downloaded = $false
    $localPackage = ''
    $tempPackageDir = ''

    if ($Package) {
        # 相对路径按"当前工作目录"解析后转成绝对路径：提权子进程会换工作目录。
        $candidate = $Package
        if (-not [System.IO.Path]::IsPathRooted($candidate)) {
            $candidate = Join-Path (Get-Location).Path $candidate
        }
        if (-not (Test-Path -LiteralPath $candidate)) { throw "找不到本地安装包: $Package" }
        $localPackage = (Resolve-Path -LiteralPath $candidate).Path
    } elseif ($script:LocalPackage) {
        # 自动发现的 U 盘离线包（提权前已定位，路径是绝对的）。
        $localPackage = $script:LocalPackage.Path
        $tempPackageDir = $script:LocalPackage.TempDir
    } elseif ($Offline) {
        Write-Fail '指定了 -Offline，但在本目录及其 package\ 子目录里没有找到安装包（observatory-agent-*.zip / agent-*.zip）。'
        Write-Fail "当前查找的位置：$((Get-PackageSearchDirs) -join ' ; ')"
        if ($script:LocalPackageError) { Write-Fail "查找过程报错：$($script:LocalPackageError)" }
        exit 5
    }

    $primary = $null
    if (-not $localPackage) {
        $primary = Resolve-BaseUrl
        if (-not $primary) { throw '两个域名都不可达' }
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
            Write-Step '使用本地安装包（没有 .sha256，跳过完整性校验）'
        }
        Write-Step "本地安装包: $packagePath (版本 $targetVersion)，不下载"
    } else {
        Write-Step '查询服务器上的受控端版本'
        $package = Invoke-WithFailover -Path '/api/v1/public/agent/latest'
        $targetVersion = $AgentVersion
        $expectedSha = ''
        $expectedSize = [long]0
        if ($package -and $package.available) {
            $targetVersion = $package.version
            $expectedSha = [string]$package.sha256
            if ($package.size_bytes) { $expectedSize = [long]$package.size_bytes }
            Write-Step "服务器提供版本: $targetVersion（$([string](Format-Bytes $expectedSize))）"
        } else {
            Write-Step "服务器没有上传安装包，使用脚本内置版本 $AgentVersion"
        }

        $packagePath = Join-Path $env:TEMP "observatory-agent-$targetVersion.zip"
        Write-Step "下载安装包 (版本 $targetVersion)"
        # 流式下载 + 可视化进度（百分比/已下载/速度/剩余时间）；不是
        # Invoke-WebRequest——它无法报告进度，26 MB 下载期间界面完全静止。
        Get-PackageWithFailover -Path "/api/v1/public/agent/$targetVersion/package" -OutFile $packagePath -ExpectedSize $expectedSize | Out-Null
        $downloaded = $true

        if ($expectedSha) {
            Write-Step '校验安装包完整性 (sha256)'
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
    # 从 bundle 里解出来的内层包也一并清掉（U 盘原件不动）。
    if ($tempPackageDir) {
        Remove-Item -LiteralPath $tempPackageDir -Recurse -Force -ErrorAction SilentlyContinue
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
