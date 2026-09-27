$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
Write-Host "=== Local Cowork Status ==="
Write-Host "[Docker]"
docker compose ps
Write-Host "[Ollama]"
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path -LiteralPath $candidate) { $ollama = Get-Item -LiteralPath $candidate }
}
if ($ollama) {
    & $ollama.Source --version
    & $ollama.Source list
    & $ollama.Source ps
} else { Write-Warning "Ollama command not found." }
Write-Host "[Ports]"
$rows = foreach ($port in 8099,3000,8000,8098) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    [PSCustomObject]@{Port=$port;State=if($listener){"LISTEN"}else{"STOPPED"};Address=($listener.LocalAddress -join ",")}
}
$rows | Format-Table -AutoSize
Write-Host "Integrated Front: http://127.0.0.1:8099"
Write-Host "Open WebUI:       http://127.0.0.1:3000"
Write-Host "Computer:         http://127.0.0.1:8000"
