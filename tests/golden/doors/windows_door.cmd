@echo off
setlocal
curl.exe -s -m 5 "http://0.0.0.0/step/s0" >nul 2>&1
rem envrec door for Windows (generated from a fixed template; the only thing that varies is the Mac's public key and address).
if "%~1"=="" (
  echo This file will do exactly the following on this computer:
  echo   1. Ask Windows for administrator permission ^(you click Yes^).
  echo   2. Install the OpenSSH Server feature from Microsoft if it is missing.
  echo   3. Set the sshd service to start MANUALLY ^(never at boot^).
  echo   4. Allow port 22 ONLY on the Private network profile.
  echo   5. Add ONE public key, valid only from 192.168.1.20, able to forward only port 8089 on this computer.
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
curl.exe -s -m 5 "http://0.0.0.0/step/elevated" >nul 2>&1
set "SSHDIR=%ProgramData%\ssh"
set "KEYLINE=from="192.168.1.20",no-agent-forwarding,no-X11-forwarding,permitopen="127.0.0.1:8089" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGRlZmdoaWprbG1ub3BxcnN0dXZ3eHl6e3x9fn+AgYKD # envrec-door"
curl.exe -s -m 5 "http://0.0.0.0/step/s1" >nul 2>&1
echo [1/7] Installing the OpenSSH Server feature if it is missing. This can take several minutes and needs an internet connection: do not close this window.
powershell -NoProfile -Command "try { if (-not (Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Where-Object State -eq 'Installed')) { Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction Stop | Out-Null }; exit 0 } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 goto :failed
sc query sshd >nul 2>&1
if errorlevel 1 (
  echo The sshd service does not exist after the install step.
  goto :failed
)
curl.exe -s -m 5 "http://0.0.0.0/step/s2" >nul 2>&1
echo [2/7] Setting the sshd service to start manually.
sc config sshd start= demand
if errorlevel 1 goto :failed
curl.exe -s -m 5 "http://0.0.0.0/step/s3" >nul 2>&1
echo [3/7] Limiting the firewall to the Private network profile.
netsh advfirewall firewall delete rule name="envrec-door-sshd" >nul 2>&1
netsh advfirewall firewall add rule name="envrec-door-sshd" dir=in action=allow protocol=TCP localport=22 profile=private
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
curl.exe -s -m 5 "http://0.0.0.0/step/s4" >nul 2>&1
echo [4/7] Adding the key and starting sshd once so it creates its host key.
net start sshd >nul 2>&1
echo %KEYLINE%>>"%AUTHFILE%"
icacls "%AUTHFILE%" /inheritance:r /grant "%ACLUSER%:F" /grant "SYSTEM:F" >nul
powershell -NoProfile -Command "$p=Join-Path $env:ProgramData 'ssh\sshd_config'; $c=@(Get-Content $p | Where-Object { $_ -notmatch 'envrec-door' }); Set-Content $p (@('PasswordAuthentication no # envrec-door','PubkeyAuthentication yes # envrec-door') + $c)"
curl.exe -s -m 5 "http://0.0.0.0/step/s5" >nul 2>&1
echo [5/7] Turning off password logins and restarting sshd.
net stop sshd >nul 2>&1
net start sshd
if errorlevel 1 goto :failed
if not exist "%SSHDIR%\ssh_host_ed25519_key.pub" (
  echo sshd started but has no ed25519 host key.
  goto :failed
)
curl.exe -s -m 5 "http://0.0.0.0/step/s6" >nul 2>&1
echo [6/7] Writing the undo file.
(
  echo @echo off
  echo net stop sshd
  echo sc config sshd start= disabled
  echo netsh advfirewall firewall delete rule name="envrec-door-sshd"
  echo netsh advfirewall firewall set rule name="OpenSSH-SSH-In-TCP" new enable=yes
  echo netsh advfirewall firewall set rule name="OpenSSH-Server-In-TCP" new enable=yes
  echo powershell -NoProfile -Command "foreach($f in @('%SSHDIR%\administrators_authorized_keys','%TARGET_PROFILE%\.ssh\authorized_keys','%SSHDIR%\sshd_config')){ if(Test-Path $f){ $k=@(Get-Content $f ^| Where-Object { $_ -notmatch 'envrec-door' }); Set-Content $f $k } }"
  echo echo envrec door removed. Delete this file and envrec-door.cmd if you wish.
  echo pause
) > "%HERE%envrec-undo.cmd"
curl.exe -s -m 5 "http://0.0.0.0/step/s7" >nul 2>&1
echo [7/7] Computing the pairing code and reporting to the Mac.
powershell -NoProfile -Command "$l=Get-Content (Join-Path $env:ProgramData 'ssh\ssh_host_ed25519_key.pub') -Raw; $b=[Convert]::FromBase64String(($l.Trim() -split ' ')[1]); $h=[System.Security.Cryptography.SHA256]::Create().ComputeHash($b); $n=[uint32]([BitConverter]::ToUInt32(@($h[3],$h[2],$h[1],$h[0]),0)); Write-Host ''; Write-Host ('PAIRING CODE: ' + ($n %% 1000000).ToString('000000')); Write-Host 'Type these 6 digits on the Mac.'"
powershell -NoProfile -Command "try { $k=(Get-Content (Join-Path $env:ProgramData 'ssh\ssh_host_ed25519_key.pub') -Raw).Trim(); $os=(Get-CimInstance Win32_OperatingSystem); $b=@{user=$env:TARGET_USER; computer=$env:COMPUTERNAME; os=($os.Caption + ' ' + $os.Version); host_key=$k} | ConvertTo-Json; Invoke-RestMethod -Method Post -Uri 'http://0.0.0.0/report' -Body $b -ContentType 'application/json' -TimeoutSec 15 | Out-Null; Write-Host 'Reported to the Mac: your user name, computer name, Windows version and the SSH host key. Nothing else.' } catch { Write-Host 'Could not reach the Mac to report. Make sure both computers are on a network where they can reach each other.' }"
echo.
echo Undo file written: %HERE%envrec-undo.cmd
pause
exit /b 0
:failed
curl.exe -s -m 5 "http://0.0.0.0/step/failed" >nul 2>&1
echo.
echo ===== THE SETUP STOPPED. Nothing was reported. Read the line above and tell the person who owns the Mac what it says. =====
echo Run envrec-undo.cmd (if it exists) to remove what was changed.
pause
exit /b 1
