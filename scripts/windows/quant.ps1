# `quant ...` on Windows: every command runs inside WSL, except `desk up` / `desk down`, which start and stop the
# QuantDesk scheduled task (the task keeps `quant desk supervise` running in the foreground inside WSL).
$ErrorActionPreference = "Stop"
$Rest = $args
$Distro = if ($env:QUANT_WSL_DISTRO) { $env:QUANT_WSL_DISTRO } else { "Ubuntu-24.04" }
$Task = "QuantDesk"

if ($Rest.Count -ge 2 -and $Rest[0] -eq "desk") {
    switch ($Rest[1]) {
        "up" {
            $t = Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
            if (-not $t) { Write-Error "Scheduled task $Task not found; run scripts\windows\setup.ps1 first." }
            if ($t.State -eq "Running") { Write-Host "QuantDesk task is already running." }
            else { Start-ScheduledTask -TaskName $Task; Write-Host "QuantDesk task started; waiting for the supervisor ..." }
            Start-Sleep -Seconds 15
            & wsl.exe -d $Distro -- quant desk status
            exit $LASTEXITCODE
        }
        "down" {
            & wsl.exe -d $Distro -- quant desk down
            Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
            Write-Host "QuantDesk stopped."
            exit 0
        }
    }
}
& wsl.exe -d $Distro -- quant @Rest
exit $LASTEXITCODE
