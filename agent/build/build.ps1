# Build the Windows agent package.
#
#   pwsh -File agent/build/build.ps1
#   pwsh -File agent/build/build.ps1 -Clean -Python C:\Python312\python.exe
#
# Produces agent/dist/observatory-agent-<version>.zip, ready to upload through the
# console (设置 -> 受控端版本) or to copy to the server's packages directory.
#
# The version is read from the repository-root VERSION file, which is the single
# source of truth shared with the server and the installer.

[CmdletBinding()]
param(
    [string]$Python = '',
    [switch]$Clean,
    [switch]$SkipInstall,
    [string]$OutputDir = '',
    # Sign the executable with signtool when a certificate thumbprint is given.
    [string]$SignThumbprint = ''
)

$ErrorActionPreference = 'Stop'

$BuildDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentDir = Split-Path -Parent $BuildDir
$RepoRoot = Split-Path -Parent $AgentDir
$VersionFile = Join-Path $RepoRoot 'VERSION'

if (-not (Test-Path $VersionFile)) { throw "VERSION file not found at $VersionFile" }
$Version = (Get-Content $VersionFile -Raw).Trim()
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "VERSION is not a semantic version: $Version" }

if (-not $Python) {
    $candidate = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path $candidate) { $Python = $candidate } else { $Python = 'python' }
}
if (-not (Test-Path $Python) -and $Python -ne 'python') { throw "python interpreter not found: $Python" }
if (-not $OutputDir) { $OutputDir = Join-Path $AgentDir 'dist' }

Write-Host '== Observatory agent build ==' -ForegroundColor Cyan
Write-Host "  python : $Python"
Write-Host "  agent  : $AgentDir"
Write-Host "  output : $OutputDir"
Write-Host "  version: $Version"

if ($Clean) {
    foreach ($path in @(
        (Join-Path $BuildDir 'build'),
        (Join-Path $BuildDir 'dist'),
        (Join-Path $AgentDir 'dist')
    )) {
        if (Test-Path $path) { Write-Host "  cleaning $path"; Remove-Item -Recurse -Force $path }
    }
}

if (-not $SkipInstall) {
    Write-Host '  installing build dependencies' -ForegroundColor Cyan
    & $Python -m pip install --quiet --upgrade pip
    & $Python -m pip install --quiet -r (Join-Path $AgentDir 'requirements.txt') pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'dependency installation failed' }
}

# A frozen agent cannot read the repo VERSION file, so it is bundled by the spec.
Write-Host '  running PyInstaller' -ForegroundColor Cyan
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath (Join-Path $BuildDir 'dist') `
    --workpath (Join-Path $BuildDir 'build') `
    (Join-Path $BuildDir 'observatory-agent.spec')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

$distDir = Join-Path $BuildDir 'dist\observatory-agent'
$exe = Join-Path $distDir 'observatory-agent.exe'
if (-not (Test-Path $exe)) { throw "expected $exe but it was not produced" }

# The updater reads VERSION from the directory next to the exe; make sure the
# freshly built bundle reports exactly the repository version.
Set-Content -Path (Join-Path $distDir 'VERSION') -Value $Version -Encoding UTF8 -NoNewline

Get-ChildItem -Path $distDir -Recurse -Include '*.pyc', '*.log', '.DS_Store' -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

if ($SignThumbprint) {
    Write-Host '  signing executable' -ForegroundColor Cyan
    $signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if (-not $signtool) { throw 'signtool.exe not found in PATH' }
    & $signtool.Source sign /sha1 $SignThumbprint /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $exe
    if ($LASTEXITCODE -ne 0) { throw 'signing failed' }
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$zipName = "observatory-agent-$Version.zip"
$zipPath = Join-Path $OutputDir $zipName
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }

Write-Host '  packaging' -ForegroundColor Cyan
# Files must sit at the zip root: the installer expands them straight into
# C:\Program Files\Observatory.
Compress-Archive -Path (Join-Path $distDir '*') -DestinationPath $zipPath -CompressionLevel Optimal

$size = (Get-Item $zipPath).Length
$hash = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLower()
$exeSize = (Get-Item $exe).Length

Write-Host ''
Write-Host '== build complete ==' -ForegroundColor Green
Write-Host "  package: $zipPath"
Write-Host ("  size   : {0:N1} MB (exe {1:N1} MB)" -f ($size / 1MB), ($exeSize / 1MB))
Write-Host "  sha256 : $hash"
Write-Host ''
Write-Host 'Next steps:' -ForegroundColor Cyan
Write-Host '  1. Upload it in the console: 设置 -> 受控端版本 -> 上传安装包'
Write-Host '     (or: scp the zip to the server and run `observatory-cli package-add`)'
Write-Host '  2. Devices update themselves on their next heartbeat. No reinstall.'
Write-Host ''
Write-Host 'Local verification:' -ForegroundColor Cyan
Write-Host "  & '$exe' --version"
Write-Host "  & '$exe' --doctor"