# Multi Auto Setup: installer for Windows 10/11. No administrator rights, nothing outside your user folder.
#
#   Download, read, run:
#       Invoke-WebRequest <url>/install.ps1 -OutFile install.ps1
#       notepad install.ps1
#       powershell -ExecutionPolicy Bypass -File .\install.ps1
#   (-ExecutionPolicy Bypass applies to THIS run only and needs no administrator.)
#   Remove everything:   powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\.mas\install.ps1 -Uninstall      (your keys only with -Purge)
#
# What it does, in order, asking before each change: (1) uv, the Python installer, if you do not have it; (2) downloads the archive of ONE pinned version and checks its SHA-256
# against the value written below (it stops on a mismatch); (3) builds the program in %USERPROFILE%\.mas; (4) creates the `mas` command and, only if you accept, adds it to the PATH
# of YOUR user; (5) asks whether this computer will be a coordinator or a node. It never touches the firewall, the network, the clock or the router, and installs only our program: no AI models.
#
# Options:   -Tag TAG -Sha256 HEX   another version (both are required: the embedded hash is only for the default version)
#            -Role node|coordinator skip the question      -Yes   answer yes to everything shown (for scripts)
#            -Home FOLDER           install there instead of %USERPROFILE%\.mas      -BaseUrl URL   another release server (https, or http on this computer)
#            -NoJoin                do not offer `mas join`  -Uninstall [-Purge]    remove (keys only with -Purge)

# PIN-BEGIN (scripts/pin_installers.py rewrites these three lines)
$DefaultTag = 'node-preview-5'
$DefaultSha256 = 'ca07beae522cdadbd1a45ada38c82510f73e16e8c45afbba14ae4b1c25fccb93'
$DefaultBase = 'https://github.com/josuecuguy1307/Multi-Auto-Setup/releases'
# PIN-END
$PyVersion = '3.13'
$UvInstallCmd = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
$ProgressPreference = 'SilentlyContinue'

# ---- options (a plain parser, so the script also works when it arrives through a pipe and has no parameters) ----------------------------------------
$Opt = @{ Tag = $DefaultTag; Sha256 = ''; Role = ''; Yes = $false; Home = ''; BaseUrl = ''; NoJoin = $false; Uninstall = $false; Purge = $false }
$i = 0
while ($i -lt $args.Count) {
    $a = [string]$args[$i]
    switch -Regex ($a) {
        '^-(Tag|Sha256|Role|Home|BaseUrl)$' { if ($i + 1 -ge $args.Count) { throw "$a needs a value" }; $Opt[$a.TrimStart('-')] = [string]$args[$i + 1]; $i += 2; continue }
        '^-(Yes|NoJoin|Uninstall|Purge)$' { $Opt[$a.TrimStart('-')] = $true; $i += 1; continue }
        default { throw "Unknown option: $a" }
    }
}
# where this script file is (empty when it arrived through a pipe). Read here, at script level: inside a function the invocation info is the function's, which has no Path (an error under strict mode).
$ScriptPath = if ($PSCommandPath) { [string]$PSCommandPath } else { '' }
$AssumeYes = [bool]$Opt.Yes
$MasHome = if ($Opt.Home) { $Opt.Home } elseif ($env:MAS_HOME) { $env:MAS_HOME } else { Join-Path $env:USERPROFILE '.mas' }
$Base = if ($Opt.BaseUrl) { $Opt.BaseUrl } elseif ($env:MAS_RELEASE_BASE) { $env:MAS_RELEASE_BASE } else { $DefaultBase }
$Base = $Base.TrimEnd('/')
$BinDir = Join-Path $MasHome 'bin'

function Say([string]$t) { Write-Host $t }
# `exit` would close the person's whole PowerShell window when this script runs through `irm | iex`, so a stop is an exception caught at the bottom.
function Die([string]$t) { throw [System.InvalidOperationException]::new("MAS_STOP:$t") }

function Ask([string]$q) {
    if ($AssumeYes) { Say "$q [y/N]: y  (-Yes)"; return $true }
    if (-not [Environment]::UserInteractive) { Say 'There is no terminal to ask you; that change was not made.'; return $false }
    $r = Read-Host "$q [y/N]"
    return ($r -match '^(y|yes)$')
}

function Test-SafeHome {
    $full = [IO.Path]::GetFullPath($MasHome).TrimEnd('\')
    if ($full -eq '' -or $full.Length -le 3 -or $full -ieq $env:USERPROFILE.TrimEnd('\')) { Die "The install folder '$MasHome' is not safe. Nothing was done." }
}

function Get-StateDir {
    $cur = Join-Path $MasHome 'current'
    if (-not (Test-Path $cur)) { return '' }
    $tag = (Get-Content $cur -Raw).Trim()
    $py = Join-Path $MasHome "releases\$tag\.venv\Scripts\python.exe"
    if (Test-Path $py) { try { return (& $py -c 'import envrec_os; print(envrec_os.default_state_dir())' 2>$null | Select-Object -First 1) } catch { } }
    return ''
}

function Get-UserPath { return [Environment]::GetEnvironmentVariable('Path', 'User') }
function Test-OnUserPath([string]$dir) {
    $p = Get-UserPath
    if (-not $p) { return $false }
    foreach ($e in $p.Split(';')) { if ($e.TrimEnd('\') -ieq $dir.TrimEnd('\')) { return $true } }
    return $false
}

function Get-Sha256([string]$file) { return (Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLower() }

function Get-Remote([string]$url, [string]$out) {
    if ($url -notmatch '^(https://|http://(127\.0\.0\.1|localhost|\[::1\])([:/]|$))') { Die "Downloads are only made over HTTPS (or http on this same computer): $url" }
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $out
}

# ---- uninstall ---------------------------------------------------------------------------------------------------------------------------------
function Invoke-Uninstall {
    Test-SafeHome
    if (-not ((Test-Path (Join-Path $MasHome 'current')) -or (Test-Path (Join-Path $MasHome 'releases')))) { Die "I cannot find an installation in $MasHome. Nothing was changed." }
    $state = Get-StateDir
    Say 'This will be removed from this computer:'
    Say '  - the Multi Auto Setup user services, if you installed them (mas service)'
    Say "  - the program folder, its environments and the command: $MasHome"
    if (Test-OnUserPath $BinDir) { Say "  - the $BinDir entry in your user PATH" }
    if ($Opt.Purge) { Say "  - YOUR KEYS, pairings and logs in $state  (-Purge)" }
    else { Say "Your keys, pairings and logs are KEPT in $state ; delete that folder by hand or use -Purge if you want to forget them." }
    Say 'uv is not touched (you may use it for other things), nor the firewall or the network.'
    if (-not (Ask 'Continue?')) { Die 'Nothing was changed.' }
    if ($Opt.Purge) {
        Say 'Deleting the keys is final: this computer would have to pair again and, if it is the coordinator, ALL the nodes too.'
        $w = if ($AssumeYes) { 'DELETE' } else { Read-Host 'Type DELETE to confirm' }
        if ($w -cne 'DELETE') { Die 'Not confirmed. Nothing was changed.' }
    }
    $mas = Join-Path $BinDir 'mas.cmd'
    if (Test-Path $mas) { foreach ($r in 'node', 'coordinator') { try { & $mas service uninstall --role $r --yes *> $null } catch { } } }
    if (Test-OnUserPath $BinDir) {
        $kept = (Get-UserPath).Split(';') | Where-Object { $_ -and ($_.TrimEnd('\') -ine $BinDir.TrimEnd('\')) }
        [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
    }
    Set-Location $env:USERPROFILE
    Remove-Item -LiteralPath $MasHome -Recurse -Force
    if ($Opt.Purge -and $state -and (Test-Path $state)) { Remove-Item -LiteralPath $state -Recurse -Force }
    Say 'Done. Multi Auto Setup was removed from this computer.'
}

# ---- install -----------------------------------------------------------------------------------------------------------------------------------
function Find-Uv {
    $c = Get-Command uv -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path $p) { return $p }
    return ''
}

function Invoke-Install {
    Test-SafeHome
    $tag = $Opt.Tag
    $sha = $Opt.Sha256
    if ($tag -ne $DefaultTag -and -not $sha) { Die "You asked for version '$tag'. The SHA-256 this installer carries is only the one for '$DefaultTag'; for another version add -Sha256 HEX (the one in its published manifest). The check is never skipped. Nothing was changed." }
    if ($tag -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') { Die "Invalid version name: $tag" }
    if (-not $sha) { $sha = $DefaultSha256 }
    if ($sha -notmatch '^[0-9a-f]{64}$') { Die '-Sha256 must be 64 lowercase hexadecimal characters.' }
    if ($Opt.Role -and $Opt.Role -notin 'node', 'coordinator') { Die '-Role must be node or coordinator' }
    $archive = "mas-$tag.tar.gz"
    $url = "$Base/download/$tag/$archive"
    $rel = Join-Path $MasHome "releases\$tag"
    $venvPy = Join-Path $rel '.venv\Scripts\python.exe'

    Say 'Multi Auto Setup: installation for your user (no administrator).'
    Say ''
    Say 'What I am going to do, asking before each change:'
    Say '  1. use uv (the Python installer); if you do not have it, I show you the official command and ask'
    Say "  2. download $url"
    Say "     and check that its SHA-256 is $sha (if it differs, I stop)"
    Say "  3. create $rel with its own Python $PyVersion environment"
    Say "  4. create the 'mas' command in $BinDir and, if you accept, add that folder to your user PATH"
    Say '  5. ask whether this computer will be a coordinator or a node'
    Say 'I do not touch the firewall, the network, the clock or the router. I do not install AI models.'
    Say ''
    Say 'Note: this script is not signed. If you downloaded it with a browser, Windows may mark it as downloaded from the Internet; that is why it is run with -ExecutionPolicy Bypass for this run only (or with Unblock-File after reading it).'
    Say ''

    $uv = Find-Uv
    if (-not $uv) {
        Say 'I cannot find uv. The official command to install it for your user is:'
        Say "    $UvInstallCmd"
        Say 'I will run it with UV_NO_MODIFY_PATH=1, so uv does NOT change your PATH (we decide about that below). Undo: delete %USERPROFILE%\.local\bin\uv.exe and uvx.exe.'
        if (-not (Ask 'Run that command now?')) { Die 'I cannot continue without uv. Nothing was changed.' }
        $env:UV_NO_MODIFY_PATH = '1'
        Invoke-Expression (Invoke-RestMethod -UseBasicParsing 'https://astral.sh/uv/install.ps1')
        $uv = Find-Uv
        if (-not $uv) { Die 'uv did not end up installed. Stopping.' }
    } else { Say "uv found: $uv" }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("mas-install-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $healthy = $false
        if ((Test-Path $venvPy) -and (Test-Path (Join-Path $rel 'RELEASE')) -and ((Get-Content (Join-Path $rel 'RELEASE') -Raw) -match $sha)) {
            & $venvPy -c 'import envrec_mas.cli' 2>$null
            if ($LASTEXITCODE -eq 0) { $healthy = $true }
        }
        if ($healthy) { Say "Version $tag is already installed and responds: I repeat without downloading (repairing the command and the pointer)." }
        else {
            if (Test-Path $rel) {
                Say "There is an incomplete installation of $tag in $rel (it is ours); I replace it."
                if (-not (Ask "Delete $rel and rebuild it?")) { Die 'Nothing was changed.' }
                Remove-Item -LiteralPath $rel -Recurse -Force
            }
            if (-not (Ask "Download and build version ${tag}?")) { Die 'Nothing was changed.' }
            Say 'Downloading...'
            $file = Join-Path $tmp $archive
            try { Get-Remote $url $file } catch { if ($_.Exception.Message.StartsWith('MAS_STOP:')) { throw }; Die "Could not download $url. Nothing was changed." }
            $got = Get-Sha256 $file
            if ($got -ne $sha) { Die "The SHA-256 does NOT match.`n  expected: $sha`n  obtained: $got`nThe download was discarded and nothing was installed." }
            Say 'SHA-256 correct.'
            $names = & tar.exe -tzf $file
            if ($LASTEXITCODE -ne 0) { Die 'The downloaded archive cannot be read. Nothing was installed.' }
            if ($names | Where-Object { $_ -match '^(/|[A-Za-z]:)' -or $_ -match '(^|/)\.\.(/|$)' -or $_ -match '\\' }) { Die 'The archive contains paths that are not allowed. Nothing was installed.' }
            $tops = $names | ForEach-Object { ($_ -split '/')[0] } | Sort-Object -Unique
            if (@($tops).Count -ne 1) { Die 'The archive must have a single root folder. Nothing was installed.' }
            $x = Join-Path $tmp 'x'
            New-Item -ItemType Directory -Path $x | Out-Null
            & tar.exe -xzf $file -C $x
            if (Get-ChildItem -LiteralPath $x -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { Die 'The archive contains links. Nothing was installed.' }
            New-Item -ItemType Directory -Force -Path (Join-Path $MasHome 'releases') | Out-Null
            Move-Item -LiteralPath (Join-Path $x $tops) -Destination $rel
            Say "Building the environment (uv will download Python $PyVersion if needed)..."
            & $uv venv --python $PyVersion (Join-Path $rel '.venv')
            $ok = ($LASTEXITCODE -eq 0)
            if ($ok) { & $uv pip install --python $venvPy -e $rel; $ok = ($LASTEXITCODE -eq 0) }
            if (-not $ok) { Remove-Item -LiteralPath $rel -Recurse -Force -ErrorAction SilentlyContinue; Die 'Could not build the environment. The partial work was deleted; nothing is left half-done.' }
            $stamp = [int][double]::Parse((Get-Date -UFormat %s))
            Set-Content -LiteralPath (Join-Path $rel 'RELEASE') -Value ('{{"tag": "{0}", "sha256": "{1}", "installed_at": {2}}}' -f $tag, $sha, $stamp) -Encoding ASCII
            & $venvPy -c 'import envrec_mas.cli'
            if ($LASTEXITCODE -ne 0) { Remove-Item -LiteralPath $rel -Recurse -Force; Die 'The built version does not start. It was deleted; nothing is left half-done.' }
        }

        $curFile = Join-Path $MasHome 'current'
        $old = if (Test-Path $curFile) { (Get-Content $curFile -Raw).Trim() } else { '' }
        Set-Content -LiteralPath "$curFile.tmp" -Value $tag -Encoding ASCII
        Move-Item -LiteralPath "$curFile.tmp" -Destination $curFile -Force
        if ($old -and $old -ne $tag) { Set-Content -LiteralPath (Join-Path $MasHome 'previous') -Value $old -Encoding ASCII }
        Set-Content -LiteralPath (Join-Path $MasHome 'uv') -Value $uv -Encoding ASCII
        # keep a copy of this installer next to the program, so the uninstaller exists even when this script arrived through a pipe
        $mine = $ScriptPath
        if (Test-Path (Join-Path $rel 'install.ps1')) { Copy-Item (Join-Path $rel 'install.ps1') (Join-Path $MasHome 'install.ps1') -Force }
        elseif ($mine -and (Test-Path $mine)) { Copy-Item $mine (Join-Path $MasHome 'install.ps1') -Force }

        # the `mas` command: a small batch file that runs the release named in `current`
        New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
        $shim = @"
@echo off
rem written by the Multi Auto Setup installer
set /p MAS_T=<"$MasHome\current"
"$MasHome\releases\%MAS_T%\.venv\Scripts\mas.exe" %*
"@
        Set-Content -LiteralPath (Join-Path $BinDir 'mas.cmd') -Value $shim -Encoding ASCII
        Say "Command installed: $(Join-Path $BinDir 'mas.cmd')"

        if (-not (Test-OnUserPath $BinDir)) {
            Say ''
            Say "$BinDir is not on your user PATH, so 'mas' would not be found when you open another window."
            Say 'I can add it to YOUR user PATH (not the system one; no administrator needed). Your current user PATH is kept, with this folder added at the end:'
            Say "    $BinDir"
            Say 'Undo: Settings > System > About > Advanced system settings > Environment Variables, or run the uninstaller (it removes it by itself).'
            if (Ask 'Add that folder to your user PATH?') {
                $p = Get-UserPath
                $new = if ($p) { $p.TrimEnd(';') + ';' + $BinDir } else { $BinDir }
                [Environment]::SetEnvironmentVariable('Path', $new, 'User')
                Say 'Done. Open a new PowerShell window.'
            } else { Say "I did not add it. Meanwhile use the full path: $(Join-Path $BinDir 'mas.cmd')" }
        }

        Say ''
        $role = $Opt.Role
        if (-not $role) {
            Say 'Will this machine be a coordinator or a node?'
            Say '  coordinator = the computer you run the network from (only one).'
            Say '  node        = a computer that joins that coordinator.'
            if ([Environment]::UserInteractive) {
                $r = Read-Host 'Type c (coordinator) or n (node)'
                if ($r -match '^(c|coordinator)$') { $role = 'coordinator' } elseif ($r -match '^(n|node)$') { $role = 'node' }
            }
        }
        if ($role) { Set-Content -LiteralPath (Join-Path $MasHome 'role') -Value $role -Encoding ASCII }
        $masCmd = Join-Path $BinDir 'mas.cmd'
        switch ($role) {
            'coordinator' {
                Say 'This computer will be the COORDINATOR.'
                Say 'A coordinator listens on ONE port of your network (TCP 7443 by default and, optionally, UDP 7444 so nodes can find it).'
                Say 'Windows will ask whether you allow that access in the firewall: you decide. This installer does not change it. To start it:'
                Say '    mas coordinator start --bind THIS_COMPUTERS_ADDRESS --web'
                Say 'To start it by itself when you sign in:  mas service install --role coordinator --bind ADDRESS'
            }
            'node' {
                Say 'This computer will be a NODE: it listens on no port, it only calls the coordinator.'
                if ($Opt.NoJoin) { Say 'When you want to join it:  mas join' }
                elseif (Ask 'Look for a coordinator on your network and join this computer now (mas join)?') {
                    & $masCmd join
                    if ($LASTEXITCODE -ne 0) { Say 'It did not get paired. You can repeat it whenever you want with:  mas join' }
                } else { Say 'When you want to join it:  mas join' }
                Say 'To start it by itself when you sign in (after pairing):  mas service install'
            }
            default { Say "You did not choose a role. When you know: 'mas join' (node) or 'mas coordinator start' (coordinator)." }
        }
        Say ''
        Say "Done. Installed version: $tag. To update later: mas update.  To remove everything: powershell -ExecutionPolicy Bypass -File `"$(Join-Path $MasHome 'install.ps1')`" -Uninstall"
    }
    finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}

$failed = $false
try {
    if ($Opt.Uninstall) { Invoke-Uninstall } else { Invoke-Install }
}
catch {
    $failed = $true
    $m = $_.Exception.Message
    if ($m.StartsWith('MAS_STOP:')) { Write-Host $m.Substring(9) -ForegroundColor Red }
    else { Write-Host "Unexpected error: $m" -ForegroundColor Red }
}
if ($failed -and $PSCommandPath) { exit 1 }
