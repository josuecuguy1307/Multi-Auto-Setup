# LINUX-CONTAINER check of install.ps1 under PowerShell 7 (not Windows): refusal paths. Run:
#   docker run --rm -v "$PWD/install.ps1:/w/install.ps1:ro" -v "$PWD/scripts/mas_pwsh_installer_check.ps1:/w/t.ps1:ro" mcr.microsoft.com/powershell:lts-debian-12 pwsh -NoProfile -File /w/t.ps1
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force /tmp/up, /tmp/bin | Out-Null
Set-Content /tmp/bin/uv "#!/bin/sh`nexit 0"; chmod +x /tmp/bin/uv
$env:PATH = "/tmp/bin:" + $env:PATH
$env:USERPROFILE = '/tmp/up'
$l = [System.Net.HttpListener]::new(); $l.Prefixes.Add('http://127.0.0.1:8765/'); $l.Start()
$job = Start-ThreadJob { param($l) while ($l.IsListening) { try { $c = $l.GetContext() } catch { break }; $b = [byte[]](1..200); $c.Response.OutputStream.Write($b, 0, $b.Length); $c.Response.Close() } } -ArgumentList $l
$env:MAS_RELEASE_BASE = 'http://127.0.0.1:8765'
Write-Output '--- wrong sha'
& pwsh -NoProfile -File /w/install.ps1 -Yes -Role node -NoJoin -Tag t1 -Sha256 ('0' * 64) 2>&1 | Select-Object -Last 6
Write-Output "exit=$LASTEXITCODE; .mas exists: $(Test-Path /tmp/up/.mas)"
Write-Output '--- other tag, no sha'
& pwsh -NoProfile -File /w/install.ps1 -Yes -Tag whatever 2>&1 | Select-Object -Last 2
Write-Output "exit=$LASTEXITCODE"
Write-Output '--- insecure url'
& pwsh -NoProfile -File /w/install.ps1 -Yes -Role node -Tag t1 -Sha256 ('0' * 64) -BaseUrl 'http://example.com/r' 2>&1 | Select-Object -Last 2
Write-Output '--- unknown option'
& pwsh -NoProfile -File /w/install.ps1 -Bogus 2>&1 | Select-Object -Last 1
$l.Stop()
Write-Output '--- through iex: the session must survive a stop'
$env:MAS_RELEASE_BASE = ''
& pwsh -NoProfile -Command 'Invoke-Expression (Get-Content -Raw /w/install.ps1); "STILL ALIVE after iex"' 2>&1 | Select-Object -Last 2
