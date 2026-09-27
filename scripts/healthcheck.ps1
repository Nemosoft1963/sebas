$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$results = @()
function Add-Result {
    param([string]$Name,[bool]$Pass,[string]$Detail)
    $script:results += [PSCustomObject]@{Check=$Name;Result=if($Pass){"PASS"}else{"FAIL"};Detail=$Detail}
}
function Test-Http {
    param([string]$Name,[string]$Url)
    try {
        $response = Invoke-WebRequest $Url -TimeoutSec 15 -UseBasicParsing
        Add-Result $Name ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) "HTTP $($response.StatusCode)"
    } catch { Add-Result $Name $false $_.Exception.Message }
}
try {
    $null = Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 5
    Add-Result "Ollama API" $true "127.0.0.1:11434"
} catch { Add-Result "Ollama API" $false $_.Exception.Message }
Test-Http "Integrated Front" "http://127.0.0.1:8099/api/health"
Test-Http "Open WebUI" "http://127.0.0.1:3000"
Test-Http "Computer" "http://127.0.0.1:8000"
try {
    $response = Invoke-RestMethod "http://127.0.0.1:8010/api/config" -TimeoutSec 15
    Add-Result "Google Publisher Browser" (-not $response.needs_setup) "setup=$($response.needs_setup) / version=$($response.version)"
} catch {
    Add-Result "Google Publisher Browser" $false $_.Exception.Message
}

$running = @(docker compose ps --services --filter status=running 2>$null)
foreach ($service in "web","open-webui","cptr","google-publisher-browser") {
    Add-Result "Container $service" ($running -contains $service) ($running -join ", ")
}
$envContent = [IO.File]::ReadAllText((Join-Path $ProjectRoot ".env"), [Text.Encoding]::UTF8)
$configuredOllamaModel = [regex]::Match($envContent, "(?m)^OLLAMA_MODEL=(.+)$").Groups[1].Value.Trim()
$workspaceValue = [regex]::Match($envContent, "(?m)^WORKSPACE_PATH=(.+)$").Groups[1].Value.Trim()
$workspace = $workspaceValue.Replace("/", "\")
Add-Result "Workspace" (Test-Path -LiteralPath $workspace) $workspace
foreach ($port in 8099,3000,8000,8010) {
    $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    $safe = $listeners.Count -gt 0 -and @($listeners | Where-Object { $_.LocalAddress -notin @("127.0.0.1","::1") }).Count -eq 0
    Add-Result "Localhost port $port" $safe (($listeners.LocalAddress | Sort-Object -Unique) -join ",")
}
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path -LiteralPath $candidate) { $ollama = Get-Item -LiteralPath $candidate }
}
if ($ollama) {
    $models = (& $ollama.Source list | Out-String)
    Add-Result "Configured Ollama model" ($configuredOllamaModel -and $models.Contains($configuredOllamaModel)) $configuredOllamaModel
    Add-Result "gpt-oss:20b" ($models -match "gpt-oss:20b") "model list"
    Add-Result "qwen3.5:9b" ($models -match "qwen3.5:9b") "model list"
} else { Add-Result "Ollama models" $false "ollama command not found" }
$results | Format-Table -AutoSize
$failed = @($results | Where-Object Result -eq "FAIL").Count
if ($failed -eq 0) { Write-Host "Health check: PASS"; exit 0 }
Write-Warning "Health check completed with $failed failure(s)."
exit 1
