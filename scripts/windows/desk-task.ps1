# Action of the QuantDesk scheduled task. Keeps `quant desk supervise` running in the foreground inside WSL (a live
# wsl.exe process also keeps the distro from being shut down when idle). A clean exit (`quant desk down`) ends the
# task; any other exit restarts the supervisor after a pause.
$Distro = if ($env:QUANT_WSL_DISTRO) { $env:QUANT_WSL_DISTRO } else { "Ubuntu-24.04" }
$Log = Join-Path $env:LOCALAPPDATA "Quant\desk-task.log"
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null

function Write-Log($msg) { Add-Content -Path $Log -Value ("{0:u} {1}" -f (Get-Date).ToUniversalTime(), $msg) }

$delay = 30
while ($true) {
    # Docker Desktop starts at sign-in; wait for its engine before starting research containers.
    for ($i = 0; $i -lt 60; $i++) {
        & wsl.exe -d $Distro -- docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 10
    }
    Write-Log "starting quant desk supervise"
    & wsl.exe -d $Distro -- quant desk supervise
    $code = $LASTEXITCODE
    Write-Log "supervisor exited with $code"
    if ($code -eq 0) { break }
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, 600)
}
