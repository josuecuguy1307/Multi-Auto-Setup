"""LEGACY (retired by docs/NETWORK_CONTRACT.md 2.5 and section 1.5; kept until Phase 1 replaces it, NOT reachable from the `mas` commands; conformance and tests/test_mas_legacy.py check that): The Windows door file: a fixed .cmd template with the Mac's PUBLIC key embedded. It is the only thing the owner runs on the Windows computer.

What it does, in order (and says so before it does anything): asks Windows for administrator permission (the owner's click is the gesture), installs the OpenSSH Server feature
if missing, sets the service to start manually, limits its firewall rule to the Private network profile, installs the one key restricted to the Mac's address (and to forwarding only
the model server's port), turns password logins off, starts the service, and prints a 6-digit pairing code from the host key. It writes an undo file next to itself that stops and
disables sshd and removes the key, the sshd_config line and the firewall rule it added. Every line it adds to a system file carries the marker `envrec-door`, which is what the undo
removes. Recommend a standard (non-admin) account for this login; the file puts the key in the right place for either kind.
"""

from __future__ import annotations

import re

MARKER = "envrec-door"
SERVER_PORT = 8089
_PUB = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/=]{40,120}( [^\r\n\"]{0,60})?$")
_IP = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")

DOOR_TEMPLATE = r"""@echo off
setlocal
curl.exe -s -m 5 "@@BASE_URL@@/step/s0" >nul 2>&1
rem envrec door for Windows (generated from a fixed template; the only thing that varies is the Mac's public key and address).
if "%~1"=="" (
  echo This file will do exactly the following on this computer:
  echo   1. Ask Windows for administrator permission ^(you click Yes^).
  echo   2. Install the OpenSSH Server feature from Microsoft if it is missing.
  echo   3. Set the sshd service to start MANUALLY ^(never at boot^).
  echo   4. Allow port 22 ONLY on the Private network profile.
  echo   5. Add ONE public key, valid only from @@MAC_IP@@, able to forward only port @@PORT@@ on this computer.
  echo   6. Turn OFF password logins for SSH.
  echo   7. Start sshd, print a 6-digit pairing code to type on the Mac, and report to the Mac your user name, computer name, Windows version and SSH host key ^(nothing else^).
  echo   8. Write envrec-undo.cmd next to this file: it removes all of this.
  echo.
  pause
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0' -ArgumentList '%USERNAME%','\"%USERPROFILE%\"','\"%~dp0\"'"
  exit /b
)
set "TARGET_USER=%~1"
set "TARGET_PROFILE=%~2"
set "HERE=%~3"
curl.exe -s -m 5 "@@BASE_URL@@/step/elevated" >nul 2>&1
set "SSHDIR=%ProgramData%\ssh"
set "KEYLINE=from="@@MAC_IP@@",no-agent-forwarding,no-X11-forwarding,permitopen="127.0.0.1:@@PORT@@" @@PUBKEY@@ # @@MARKER@@"
curl.exe -s -m 5 "@@BASE_URL@@/step/s1" >nul 2>&1
echo [1/7] Installing the OpenSSH Server feature if it is missing. This can take several minutes and needs an internet connection: do not close this window.
powershell -NoProfile -Command "try { if (-not (Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Where-Object State -eq 'Installed')) { Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction Stop | Out-Null }; exit 0 } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 goto :failed
sc query sshd >nul 2>&1
if errorlevel 1 (
  echo The sshd service does not exist after the install step.
  goto :failed
)
curl.exe -s -m 5 "@@BASE_URL@@/step/s2" >nul 2>&1
echo [2/7] Setting the sshd service to start manually.
sc config sshd start= demand
if errorlevel 1 goto :failed
curl.exe -s -m 5 "@@BASE_URL@@/step/s3" >nul 2>&1
echo [3/7] Limiting the firewall to the Private network profile.
netsh advfirewall firewall delete rule name="@@MARKER@@-sshd" >nul 2>&1
netsh advfirewall firewall add rule name="@@MARKER@@-sshd" dir=in action=allow protocol=TCP localport=22 profile=private
netsh advfirewall firewall set rule name="OpenSSH-SSH-In-TCP" new enable=no >nul 2>&1
netsh advfirewall firewall set rule name="OpenSSH-Server-In-TCP" new enable=no >nul 2>&1
net localgroup administrators | findstr /i /x /c:"%TARGET_USER%" >nul 2>&1
if %errorlevel%==0 (
  set "AUTHFILE=%SSHDIR%\administrators_authorized_keys"
  set "ACLUSER=Administrators"
) else (
  if not exist "%TARGET_PROFILE%\.ssh" mkdir "%TARGET_PROFILE%\.ssh"
  set "AUTHFILE=%TARGET_PROFILE%\.ssh\authorized_keys"
  set "ACLUSER=%TARGET_USER%"
)
curl.exe -s -m 5 "@@BASE_URL@@/step/s4" >nul 2>&1
echo [4/7] Adding the key and starting sshd once so it creates its host key.
net start sshd >nul 2>&1
echo %KEYLINE%>>"%AUTHFILE%"
icacls "%AUTHFILE%" /inheritance:r /grant "%ACLUSER%:F" /grant "SYSTEM:F" >nul
powershell -NoProfile -Command "$p=Join-Path $env:ProgramData 'ssh\sshd_config'; $c=@(Get-Content $p | Where-Object { $_ -notmatch '@@MARKER@@' }); Set-Content $p (@('PasswordAuthentication no # @@MARKER@@','PubkeyAuthentication yes # @@MARKER@@') + $c)"
curl.exe -s -m 5 "@@BASE_URL@@/step/s5" >nul 2>&1
echo [5/7] Turning off password logins and restarting sshd.
net stop sshd >nul 2>&1
net start sshd
if errorlevel 1 goto :failed
if not exist "%SSHDIR%\ssh_host_ed25519_key.pub" (
  echo sshd started but has no ed25519 host key.
  goto :failed
)
curl.exe -s -m 5 "@@BASE_URL@@/step/s6" >nul 2>&1
echo [6/7] Writing the undo file.
(
  echo @echo off
  echo net stop sshd
  echo sc config sshd start= disabled
  echo netsh advfirewall firewall delete rule name="@@MARKER@@-sshd"
  echo netsh advfirewall firewall set rule name="OpenSSH-SSH-In-TCP" new enable=yes
  echo netsh advfirewall firewall set rule name="OpenSSH-Server-In-TCP" new enable=yes
  echo powershell -NoProfile -Command "foreach($f in @('%SSHDIR%\administrators_authorized_keys','%TARGET_PROFILE%\.ssh\authorized_keys','%SSHDIR%\sshd_config')){ if(Test-Path $f){ $k=@(Get-Content $f ^| Where-Object { $_ -notmatch '@@MARKER@@' }); Set-Content $f $k } }"
  echo echo envrec door removed. Delete this file and envrec-door.cmd if you wish.
  echo pause
) > "%HERE%envrec-undo.cmd"
curl.exe -s -m 5 "@@BASE_URL@@/step/s7" >nul 2>&1
echo [7/7] Computing the pairing code and reporting to the Mac.
powershell -NoProfile -Command "$l=Get-Content (Join-Path $env:ProgramData 'ssh\ssh_host_ed25519_key.pub') -Raw; $b=[Convert]::FromBase64String(($l.Trim() -split ' ')[1]); $h=[System.Security.Cryptography.SHA256]::Create().ComputeHash($b); $n=[uint32]([BitConverter]::ToUInt32(@($h[3],$h[2],$h[1],$h[0]),0)); Write-Host ''; Write-Host ('PAIRING CODE: ' + ($n %% 1000000).ToString('000000')); Write-Host 'Type these 6 digits on the Mac.'"
powershell -NoProfile -Command "try { $k=(Get-Content (Join-Path $env:ProgramData 'ssh\ssh_host_ed25519_key.pub') -Raw).Trim(); $os=(Get-CimInstance Win32_OperatingSystem); $b=@{user=$env:TARGET_USER; computer=$env:COMPUTERNAME; os=($os.Caption + ' ' + $os.Version); host_key=$k} | ConvertTo-Json; Invoke-RestMethod -Method Post -Uri '@@REPORT_URL@@' -Body $b -ContentType 'application/json' -TimeoutSec 15 | Out-Null; Write-Host 'Reported to the Mac: your user name, computer name, Windows version and the SSH host key. Nothing else.' } catch { Write-Host 'Could not reach the Mac to report. Make sure both computers are on a network where they can reach each other.' }"
echo.
echo Undo file written: %HERE%envrec-undo.cmd
pause
exit /b 0
:failed
curl.exe -s -m 5 "@@BASE_URL@@/step/failed" >nul 2>&1
echo.
echo ===== THE SETUP STOPPED. Nothing was reported. Read the line above and tell the person who owns the Mac what it says. =====
echo Run envrec-undo.cmd (if it exists) to remove what was changed.
pause
exit /b 1
"""


def render_door(mac_ip: str, public_key: str, report_url: str = "http://0.0.0.0/report") -> str:
    """The door file with the Mac's address and PUBLIC key embedded. Refuses anything that is not a plain ed25519 public key line or an IPv4 address."""
    if not _IP.match(mac_ip) or any(int(p) > 255 for p in mac_ip.split(".")):
        raise ValueError("the Mac address must be an IPv4 address")
    key = public_key.strip()
    if "PRIVATE" in key or not _PUB.match(key):
        raise ValueError("only a single ssh-ed25519 PUBLIC key line may be embedded")
    if not re.fullmatch(r"http://(\d{1,3}\.){3}\d{1,3}:\d{1,5}/[A-Za-z0-9_\-]{16,64}/report|http://0\.0\.0\.0/report", report_url):
        raise ValueError("the report address must be the Mac's own http address with its one-time token")
    key = " ".join(key.split()[:2])  # the comment of the key is not carried: it could hold a user name
    text = DOOR_TEMPLATE.replace("@@MAC_IP@@", mac_ip).replace("@@PUBKEY@@", key).replace("@@MARKER@@", MARKER).replace("@@PORT@@", str(SERVER_PORT)).replace("@@REPORT_URL@@", report_url).replace("@@BASE_URL@@", report_url.rsplit("/", 1)[0])
    return text.replace("\r\n", "\n").replace("\n", "\r\n")  # a .cmd file wants CRLF
