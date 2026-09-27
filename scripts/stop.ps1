$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
try {
    $null = Invoke-RestMethod "http://127.0.0.1:8099/api/control/stop" -Method Post -TimeoutSec 180
    Write-Host "Front AI unloaded."
} catch {
    Write-Warning "Front AI was already stopped or unavailable."
}
Get-NetTCPConnection -LocalPort 8098 -State Listen -ErrorAction SilentlyContinue |
ForEach-Object {
    $micProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" -ErrorAction SilentlyContinue
    if ($micProcess.CommandLine -match "app\.mic_bridge:app" -and $micProcess.CommandLine -match "LOCALSAPORTER") {
        Stop-Process -Id $_.OwningProcess -Force
        Write-Host "Stopped microphone bridge process $($_.OwningProcess)."
    }
}
docker compose stop
if ($LASTEXITCODE -ne 0) { Write-Warning "docker compose stop reported an error." }
Write-Host "Local Cowork stopped. Named volumes and workspace were preserved."
