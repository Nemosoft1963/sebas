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
$proxyConfigured = [regex]::IsMatch($envContent, '(?m)^SEBAS_PROXY_TOKEN=[A-Za-z0-9._~+=-]{16,}$') -and [regex]::IsMatch($envContent, '(?m)^PROXY_BIND_IP=[^\r\n]+')
if ($proxyConfigured) {
    $composeConfig = docker compose -f docker-compose.yml -f docker-compose.lan-off.yml -f docker-compose.proxy.yml config --format json 2>$null | ConvertFrom-Json
} else {
    $composeConfig = docker compose config --format json 2>$null | ConvertFrom-Json
}
$webBindings = @($composeConfig.services.web.ports | Where-Object { [string]$_.published -eq "8099" } | ForEach-Object { [string]$_.host_ip })
$proxyBindings = @()
if ($proxyConfigured) {
    $proxyBindings = @($composeConfig.services.'sebas-lan-proxy'.ports | Where-Object { [string]$_.published -eq "8099" } | ForEach-Object { [string]$_.host_ip })
}
$allowed8099 = @($webBindings + $proxyBindings)
$wildcardAllowed = @($allowed8099 | Where-Object { $_ -in @("0.0.0.0", "::", "[::]", "*") }).Count -gt 0
Add-Result "Compose web loopback only" ($webBindings.Count -eq 1 -and $webBindings[0] -eq "127.0.0.1") ($webBindings -join ",")
if ($proxyConfigured) {
    Add-Result "Compose proxy LAN only" ($proxyBindings.Count -eq 1 -and $proxyBindings[0] -notin @("127.0.0.1", "0.0.0.0", "::", "[::]", "*", "")) ($proxyBindings -join ",")
}
function Check-RuntimeBinding {
    param([string]$Name, [string]$PortKey, [string]$ExpectedIp)
    $raw = docker inspect $Name --format '{{json .NetworkSettings.Ports}}' 2>$null
    $ports = $raw | ConvertFrom-Json
    $actual = @($ports.PSObject.Properties[$PortKey].Value | ForEach-Object { $_.HostIp })
    Add-Result "Runtime binding $Name" ($actual.Count -eq 1 -and $actual[0] -eq $ExpectedIp) ($actual -join ",")
}
Check-RuntimeBinding "local-voice-ai-web" "8000/tcp" "127.0.0.1"
if ($proxyConfigured) { Check-RuntimeBinding "local-cowork-sebas-lan-proxy" "8080/tcp" $proxyBindings[0] }
if ($proxyConfigured) {
    $proxyState = docker inspect local-cowork-sebas-lan-proxy --format '{{.State.Health.Status}}' 2>$null
    Add-Result "Proxy healthy" ($proxyState -eq "healthy") "$proxyState"
} else {
    $proxyExists = @(docker ps --filter name=local-cowork-sebas-lan-proxy --format '{{.Names}}' 2>$null).Count -gt 0
    Add-Result "Proxy configuration" (-not $proxyExists) "proxy not configured"
}
foreach ($port in 8099,3000,8000,8010) {
    $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    # 8099 intentionally permits the configured LAN address; all other ports remain loopback-only.
    $allowed = if ($port -eq 8099) { $allowed8099 } else { @("127.0.0.1","::1") }
    $safe = $listeners.Count -gt 0 -and @($listeners | Where-Object { $_.LocalAddress -notin $allowed }).Count -eq 0
    if ($port -eq 8099 -and $wildcardAllowed) { $safe = $false }
    Add-Result "Localhost port $port / allowed bindings" $safe (($listeners.LocalAddress | Sort-Object -Unique) -join ",")
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
