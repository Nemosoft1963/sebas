$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

$tokenPresent = -not [string]::IsNullOrWhiteSpace($env:SEBAS_PROXY_TOKEN)
if (-not $tokenPresent) {
    $envFile = Join-Path $ProjectRoot '.env'
    if (Test-Path -LiteralPath $envFile) {
        $tokenPresent = [regex]::IsMatch([IO.File]::ReadAllText($envFile), '(?m)^SEBAS_PROXY_TOKEN=[A-Za-z0-9._~+=-]{16,}$')
    }
}
if (-not $tokenPresent) { throw 'SEBAS_PROXY_TOKEN is required in the process environment or .env.' }
Write-Host "Validating the protected base compose plus LAN-off and authenticated proxy overlays..."
docker compose -f docker-compose.yml -f docker-compose.lan-off.yml -f docker-compose.proxy.yml config --quiet
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$config = docker compose -f docker-compose.yml -f docker-compose.lan-off.yml -f docker-compose.proxy.yml config --format json | ConvertFrom-Json
$webIps = @($config.services.web.ports | Where-Object { [string]$_.published -eq '8099' } | ForEach-Object { [string]$_.host_ip })
$proxyIps = @($config.services.'sebas-lan-proxy'.ports | Where-Object { [string]$_.published -eq '8099' } | ForEach-Object { [string]$_.host_ip })
if ($webIps.Count -ne 1 -or $webIps[0] -ne '127.0.0.1' -or $proxyIps.Count -ne 1 -or $proxyIps[0] -in @('', '127.0.0.1', '0.0.0.0', '::', '[::]', '*')) {
    throw 'Unsafe 8099 binding in effective Compose configuration.'
}
Write-Warning "The web service must expose only 127.0.0.1:8099; the proxy alone must expose the LAN address on 8099."
Write-Warning "Only approved collection endpoints are proxied. Other paths are denied. Token contents will not be displayed."
$answer = Read-Host "Start/update the composed services now? [y/N]"
if ($answer -notmatch '^(?i:y|yes)$') {
    Write-Host "Configuration validated; no services were changed."
    exit 0
}
docker compose -f docker-compose.yml -f docker-compose.lan-off.yml -f docker-compose.proxy.yml up -d --no-deps web
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
docker compose -f docker-compose.yml -f docker-compose.lan-off.yml -f docker-compose.proxy.yml up -d --build --no-deps sebas-lan-proxy
exit $LASTEXITCODE
