<#
.SYNOPSIS
  One-time Windows setup for the Quant research desk (WSL2 + Docker Desktop).

.DESCRIPTION
  Run in an elevated PowerShell (needed for powercfg and the WSL install):
    powershell -ExecutionPolicy Bypass -File setup.ps1
  Safe to re-run; every step checks before it changes anything.

  1. WSL distro (default Ubuntu-24.04) present; Docker Desktop reachable from it (WSL integration on).
  2. Sleep and hibernate off while on AC power.
  3. Repository cloned or fast-forwarded inside WSL (Linux filesystem, not /mnt/c); uv environment synced;
     `quant` linked to /usr/local/bin.
  4. `quant desk setup` (interactive: mandate thresholds, model key with hidden input, standing authorization)
     and `quant desk check` (the isolation probe must pass before research starts; paper trading runs anyway).
  5. Scheduled task QuantDesk (at sign-in) that keeps `quant desk supervise` running; quant.cmd on PATH.

.PARAMETER AtStartup
  Also start at boot without signing in (S4U logon). Needs Docker Desktop to start without a session, which
  it does not do by default; leave off unless you have verified that on this machine.
#>
param(
    [string]$Distro = "Ubuntu-24.04",
    [string]$Repo = "https://github.com/leoq77777/Quant.git",
    [string]$Branch = "main",
    [string]$Dir = "Quant",
    [switch]$SkipDeskSetup,
    [switch]$AtStartup
)
$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Wsl([string]$cmd) { & wsl.exe -d $Distro -- bash -lc $cmd | Out-Host; return $LASTEXITCODE }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this script in an elevated PowerShell (Run as administrator)." }

Step "WSL distro $Distro"
$distros = (& wsl.exe -l -q) -replace "`0", "" | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() }
if ($distros -notcontains $Distro) {
    & wsl.exe --install -d $Distro
    Write-Host "Installed $Distro. Finish its first start (create the Linux user), reboot if asked, then re-run setup.ps1."
    exit 0
}
$linuxUser = (& wsl.exe -d $Distro -- whoami).Trim()
Write-Host "WSL user: $linuxUser"

Step "Docker Desktop (WSL integration)"
if ((Wsl "docker info >/dev/null 2>&1") -ne 0) {
    Write-Host "docker is not reachable from $Distro." -ForegroundColor Yellow
    Write-Host "Install/start Docker Desktop, enable Settings > General > 'Start Docker Desktop when you sign in',"
    Write-Host "and Settings > Resources > WSL integration > $Distro. Then re-run setup.ps1."
    exit 1
}
Write-Host "docker OK"

Step "Power: no sleep / hibernate on AC"
& powercfg /change standby-timeout-ac 0
& powercfg /change hibernate-timeout-ac 0
& powercfg /hibernate off
Write-Host "sleep and hibernate disabled on AC power"

Step "Repository ~/$Dir inside WSL"
$clone = "if [ -d ~/$Dir/.git ]; then git -C ~/$Dir fetch origin $Branch && git -C ~/$Dir checkout $Branch && " +
         "git -C ~/$Dir pull --ff-only origin $Branch; else git clone -b $Branch $Repo ~/$Dir; fi"
if ((Wsl "command -v git >/dev/null || (sudo apt-get update && sudo apt-get install -y git curl); $clone") -ne 0) {
    throw "git clone/pull failed (private repo? run 'gh auth login' or set up a token inside WSL and re-run)"
}
if ((Wsl "bash ~/$Dir/scripts/linux/install.sh") -ne 0) { throw "install.sh failed" }
& wsl.exe -d $Distro -u root -- ln -sf "/home/$linuxUser/$Dir/scripts/linux/quant-wsl.sh" /usr/local/bin/quant
if ((Wsl "quant --help >/dev/null") -ne 0) { throw "quant is not runnable inside WSL" }

Step "Windows command: quant.cmd"
$bin = Join-Path $env:LOCALAPPDATA "Quant\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$src = "\\wsl.localhost\$Distro\home\$linuxUser\$Dir\scripts\windows"
foreach ($f in "quant.cmd", "quant.ps1", "desk-task.ps1") { Copy-Item -Force (Join-Path $src $f) (Join-Path $bin $f) }
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ";") -notcontains $bin) {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User")
    Write-Host "added $bin to your PATH (open a new terminal to use 'quant')"
}
[Environment]::SetEnvironmentVariable("QUANT_WSL_DISTRO", $Distro, "User")

if (-not $SkipDeskSetup) {
    Step "quant desk setup (interactive)"
    & wsl.exe -d $Distro -- quant desk setup
    if ($LASTEXITCODE -ne 0) { throw "quant desk setup failed" }
    Step "quant desk check"
    & wsl.exe -d $Distro -- quant desk check
    if ($LASTEXITCODE -ne 0) {
        Write-Host "desk check reported problems (see above). Paper trading will still run; research stays off" `
                   "until the isolation probe passes. Fix and re-run: quant desk check" -ForegroundColor Yellow
    }
}

Step "Scheduled task QuantDesk"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$bin\desk-task.ps1`""
$triggers = @(New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME")
$logon = "Interactive"
if ($AtStartup) { $triggers += New-ScheduledTaskTrigger -AtStartup; $logon = "S4U" }
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType $logon -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "QuantDesk" -Action $action -Trigger $triggers -Principal $principal `
    -Settings $settings -Force | Out-Null
Write-Host "registered (starts at sign-in$(if ($AtStartup) {' and at boot'}))"

Write-Host "`nDone. In a new terminal:" -ForegroundColor Green
Write-Host "  quant desk up        # start (also starts automatically at sign-in)"
Write-Host "  quant desk status    # supervisor, fleet, research loop, authorization, isolation"
Write-Host "  quant desk logs research -f"
Write-Host "  quant desk report --print"
Write-Host "  quant desk down"
