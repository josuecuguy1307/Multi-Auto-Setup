# Start Docker Desktop on this Windows dev host and wait for the engine (PROGRESS.md 2026-11-19, NEEDS_HUMAN 7).
#
# Root cause found 2026-11-19: Docker Desktop had been started from inside the Claude desktop app (an MSIX package). It then inherits the
# package's %LOCALAPPDATA% virtualization (new folders land in %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Local) and the AF_UNIX socket
# files it creates (Docker\run\dockerInference, docker-secrets-engine\engine.sock, ...) can no longer be opened or deleted from Windows
# ("The file cannot be accessed by the system"). Docker removes its old socket before listening, fails, and the engine never comes up
# (no dockerDesktopLinuxEngine pipe).
#
# RULE: start Docker Desktop from the Start menu or from a normal PowerShell window, NEVER from a Claude session or its tools.
#
# This script is the recovery path: it quits Docker, removes stale sockets through WSL (which can unlink them), starts Docker Desktop and
# waits until `docker info` answers. Run it from a normal PowerShell window (not from Claude):
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\start-docker.ps1 [-TimeoutSec 240] [-Distro Ubuntu]

param([int]$TimeoutSec = 240, [string]$Distro = "Ubuntu")

$ErrorActionPreference = "Stop"
$local = $env:LOCALAPPDATA
$exe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
if (-not (Test-Path $exe)) { throw "Docker Desktop not found at $exe" }

Write-Host "1/4 Quitting Docker Desktop (if running)..."
Get-Process | Where-Object { $_.ProcessName -match '^(Docker Desktop|docker-desktop|com\.docker\..*|docker-ai)$' } |
    Stop-Process -Force -Confirm:$false -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "2/4 Removing stale socket files through WSL ($Distro)..."
$stale = @()
foreach ($dir in @("$local\Docker\run", "$local\docker-secrets-engine")) {
    if (Test-Path $dir) {
        $stale += Get-ChildItem $dir -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue | Where-Object { -not $_.PSIsContainer }
    }
}
foreach ($f in $stale) {
    $p = $f.FullName -replace '\\', '/'
    $linux = "/mnt/" + $p.Substring(0, 1).ToLower() + $p.Substring(2)
    wsl.exe -d $Distro -u root -- rm -f -- "$linux"
    if (Test-Path -LiteralPath $f.FullName) { throw "ERROR could not remove $($f.FullName)" }
    Write-Host "    removed $($f.Name)"
}
$err = Join-Path $local "Docker\backend.error.json"
if (Test-Path $err) { Remove-Item $err -Force }
wsl.exe --shutdown

Write-Host "3/4 Starting Docker Desktop..."
Start-Process $exe
$t0 = Get-Date
Write-Host "4/4 Waiting for the engine (up to $TimeoutSec s)..."
while (((Get-Date) - $t0).TotalSeconds -lt $TimeoutSec) {
    Start-Sleep -Seconds 5
    if (Test-Path $err) {
        $m = [regex]::Match((Get-Content $err -Raw), '"description":"([^"]{0,300})').Groups[1].Value
        throw "ERROR Docker backend failed: $m"
    }
    $job = Start-Job { docker info --format '{{.ServerVersion}} ({{.OperatingSystem}}, {{.NCPU}} CPUs)' 2>$null }
    if (Wait-Job $job -Timeout 10) {
        $o = Receive-Job $job
        Remove-Job $job -Force
        if ($o) { Write-Host "OK engine $o after $([int]((Get-Date) - $t0).TotalSeconds) s"; exit 0 }
    } else { Remove-Job $job -Force }
}
throw "ERROR the engine did not answer within $TimeoutSec s"
