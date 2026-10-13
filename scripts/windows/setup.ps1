<#
.SYNOPSIS
  One-time Windows host preparation for the ZQR desk (WSL2 + Docker Desktop).

.DESCRIPTION
  powershell -ExecutionPolicy Bypass -File setup.ps1     (elevated for the power settings; safe to re-run)

  The desk itself is a Docker container (zqr-desk, restart unless-stopped) that `zqr release` ships from the
  development machine; Docker Desktop restarts it after sign-in. This script only prepares the host:
  1. WSL distro present and Docker Desktop reachable from it (WSL integration on).
  2. Sleep and hibernate off while on AC power.
  3. `zqr` inside WSL linked to /usr/local/bin (after the first `zqr release`), and zqr.cmd on the Windows PATH.
  4. Removes the pre-container setup: the QuantDesk scheduled task and %LOCALAPPDATA%\Quant\bin.
#>
param([string]$Distro = "Ubuntu-24.04")
$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

Step "WSL distro $Distro"
$distros = (& wsl.exe -l -q) -replace "`0", "" | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() }
if ($distros -notcontains $Distro) {
    & wsl.exe --install -d $Distro
    Write-Host "Installed $Distro. Finish its first start (create the Linux user), reboot if asked, then re-run."
    exit 0
}
$linuxUser = (& wsl.exe -d $Distro -- whoami).Trim()

Step "Docker Desktop"
& wsl.exe -d $Distro -- docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "docker is not reachable from $Distro. In Docker Desktop enable Settings > General > 'Start Docker" `
               "Desktop when you sign in' and Settings > Resources > WSL integration > $Distro, then re-run." `
               -ForegroundColor Yellow
    exit 1
}
Write-Host "docker OK (containers with a restart policy come back when Docker Desktop starts)"

Step "Power: no sleep / hibernate on AC"
& powercfg /change standby-timeout-ac 0
& powercfg /change hibernate-timeout-ac 0
if ($isAdmin) { & powercfg /hibernate off } else { Write-Host "not elevated: hibernate left as is" }

Step "zqr command"
& wsl.exe -d $Distro -u root -- sh -c "rm -f /usr/local/bin/quant; ln -sf /home/$linuxUser/.local/bin/zqr /usr/local/bin/zqr"
$bin = Join-Path $env:LOCALAPPDATA "ZQR\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
Set-Content -Encoding ascii -Path (Join-Path $bin "zqr.cmd") -Value @(
    "@echo off",
    "rem zqr on Windows: forwards to the host wrapper inside WSL (installed by zqr release).",
    "wsl.exe -d $Distro -- zqr %*",
    "exit /b %ERRORLEVEL%")
$old = Join-Path $env:LOCALAPPDATA "Quant\bin"
$userPath = ([Environment]::GetEnvironmentVariable("Path", "User") -split ";") | Where-Object { $_ -and $_ -ne $old }
if ($userPath -notcontains $bin) { $userPath += $bin; Write-Host "added $bin to PATH (open a new terminal)" }
[Environment]::SetEnvironmentVariable("Path", ($userPath -join ";"), "User")

Step "Remove the pre-container setup"
if (Get-ScheduledTask -TaskName "QuantDesk" -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName "QuantDesk" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "QuantDesk" -Confirm:$false
    Write-Host "unregistered scheduled task QuantDesk"
}
if (Test-Path (Split-Path $old)) { Remove-Item -Recurse -Force (Split-Path $old); Write-Host "removed $(Split-Path $old)" }
[Environment]::SetEnvironmentVariable("QUANT_WSL_DISTRO", $null, "User")

Write-Host "`nDone. From the development machine: zqr release   Then here: zqr status" -ForegroundColor Green
