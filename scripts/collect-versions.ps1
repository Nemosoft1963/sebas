$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
Write-Host "=== Version collection ==="
docker --version
docker compose version
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path -LiteralPath $candidate) { $ollama = Get-Item -LiteralPath $candidate }
}
if ($ollama) { & $ollama.Source --version; & $ollama.Source list }
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv }
docker image inspect ghcr.io/open-webui/open-webui:main --format "Open WebUI: {{index .RepoDigests 0}}" 2>$null
docker image inspect ghcr.io/open-webui/computer:latest --format "Computer: {{index .RepoDigests 0}}" 2>$null
