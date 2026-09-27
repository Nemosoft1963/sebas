param([switch]$NoMicBridge,[switch]$BuildFront,[switch]$Check)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath ".env") -or ([IO.File]::ReadAllText((Join-Path $ProjectRoot ".env")) -notmatch "(?m)^WEBUI_SECRET_KEY=.+$")) {
    & (Join-Path $PSScriptRoot "setup.ps1")
}
docker compose config | Out-Null
if ($LASTEXITCODE -ne 0) { throw "docker compose config failed." }
if ($BuildFront) { docker compose up -d --build } else { docker compose up -d }
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed." }

if (-not $NoMicBridge) {
    $listener = Get-NetTCPConnection -LocalPort 8098 -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) { & (Join-Path $PSScriptRoot "start-mic-bridge.ps1") }
}
$frontReady = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $null = Invoke-RestMethod "http://127.0.0.1:8099/api/health" -TimeoutSec 3
        $frontReady = $true
        break
    } catch { Start-Sleep -Seconds 1 }
}
if ($frontReady) {
    try {
        $null = Invoke-RestMethod "http://127.0.0.1:8099/api/control/start" -Method Post -TimeoutSec 180
    } catch {
        Write-Warning "Front AI start request failed: $($_.Exception.Message)"
    }
} else {
    Write-Warning "Integrated front did not become ready within 60 seconds."
}
docker compose ps
Write-Host ""
Write-Host "Integrated Front: http://127.0.0.1:8099"
Write-Host "Open WebUI:       http://127.0.0.1:3000"
Write-Host "Computer:         http://127.0.0.1:8000"
Write-Host "Workspace:        /workspace"
if ($Check) { & (Join-Path $PSScriptRoot "healthcheck.ps1") }
