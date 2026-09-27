param([switch]$SkipQwen)
$ErrorActionPreference = "Stop"
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path -LiteralPath $candidate) { $ollama = Get-Item -LiteralPath $candidate }
}
if (-not $ollama) { throw "Ollama is not installed or not available." }
$models = @("gpt-oss:20b")
if (-not $SkipQwen) { $models += "qwen3.5:9b" }
foreach ($model in $models) {
    Write-Host "Pulling $model ..."
    & $ollama.Source pull $model
    if ($LASTEXITCODE -ne 0) { throw "Failed to pull $model" }
}
& $ollama.Source list
