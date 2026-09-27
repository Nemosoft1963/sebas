$ErrorActionPreference = "Continue"
Write-Host "=== Local Cowork Preflight (read only) ==="
Get-ComputerInfo | Select-Object WindowsProductName,WindowsVersion,OsArchitecture
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
} else { Write-Warning "nvidia-smi not found." }
if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker --version
    docker compose version
} else { Write-Warning "Docker not found." }
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path -LiteralPath $candidate) { $ollama = Get-Item -LiteralPath $candidate }
}
if ($ollama) {
    & $ollama.Source --version
    & $ollama.Source list
} else { Write-Warning "Ollama not found." }
Get-PSDrive -PSProvider FileSystem | Select-Object Name,@{N="UsedGB";E={[Math]::Round($_.Used/1GB,1)}},@{N="FreeGB";E={[Math]::Round($_.Free/1GB,1)}}
Write-Host "No changes were made."
