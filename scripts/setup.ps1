$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Utf8 = [Text.UTF8Encoding]::new($false)
$requiredDirs = @("workspace\inbox","workspace\projects","workspace\knowledge","workspace\output","workspace\temp","workspace\.cptr","backups","logs")
foreach ($dir in $requiredDirs) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot $dir) | Out-Null
}

$envPath = Join-Path $ProjectRoot ".env"
$examplePath = Join-Path $ProjectRoot ".env.example"
if (Test-Path -LiteralPath $envPath) {
    $content = [IO.File]::ReadAllText($envPath, [Text.Encoding]::UTF8)
} else {
    $content = [IO.File]::ReadAllText($examplePath, [Text.Encoding]::UTF8)
}

function Ensure-Setting {
    param([string]$Name, [string]$Value)
    if ($script:content -notmatch "(?m)^$([regex]::Escape($Name))=") {
        $script:content = $script:content.TrimEnd() + [Environment]::NewLine + "$Name=$Value" + [Environment]::NewLine
    }
}

Ensure-Setting "LOCAL_COWORK_ROOT" (($env:USERPROFILE -replace "\\", "/") + "/LocalCowork")
Ensure-Setting "WORKSPACE_PATH" (($env:USERPROFILE -replace "\\", "/") + "/LocalCowork/workspace")
Ensure-Setting "OPEN_WEBUI_PORT" "3000"
Ensure-Setting "CPTR_PORT" "8000"
Ensure-Setting "OPEN_WEBUI_TAG" "main"
Ensure-Setting "CPTR_TAG" "latest"
Ensure-Setting "CPTR_AUDIT_LOG_LEVEL" "METADATA"
Ensure-Setting "CPTR_LOG_LEVEL" "INFO"
Ensure-Setting "CPTR_LOG_FORMAT" "text"

$secretMatch = [regex]::Match($content, "(?m)^WEBUI_SECRET_KEY=(.*)$")
if (-not $secretMatch.Success -or [string]::IsNullOrWhiteSpace($secretMatch.Groups[1].Value) -or $secretMatch.Groups[1].Value -eq "CHANGE_ME") {
    $bytes = New-Object byte[] 48
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $secret = [Convert]::ToBase64String($bytes)
    if ($secretMatch.Success) {
        $content = [regex]::Replace($content, "(?m)^WEBUI_SECRET_KEY=.*$", "WEBUI_SECRET_KEY=$secret")
    } else {
        $content = $content.TrimEnd() + [Environment]::NewLine + "WEBUI_SECRET_KEY=$secret" + [Environment]::NewLine
    }
    Write-Host "Generated WEBUI_SECRET_KEY in .env (value hidden)."
}
$originalAttributes = if (Test-Path -LiteralPath $envPath) { [IO.File]::GetAttributes($envPath) } else { $null }
try {
    if ($null -ne $originalAttributes) { [IO.File]::SetAttributes($envPath, [IO.FileAttributes]::Normal) }
    [IO.File]::WriteAllText($envPath, $content, $Utf8)

    $workspaceValue = [regex]::Match($content, "(?m)^WORKSPACE_PATH=(.+)$").Groups[1].Value.Trim()
    $runtimeWorkspace = $workspaceValue.Replace("/", "\")
    foreach ($dir in "inbox","projects","knowledge","output","temp",".cptr") {
        New-Item -ItemType Directory -Force -Path (Join-Path $runtimeWorkspace $dir) | Out-Null
    }
    $rulesSource = Join-Path $ProjectRoot "workspace\README.md"
    $rulesTarget = Join-Path $runtimeWorkspace "README.md"
    if (-not (Test-Path -LiteralPath $rulesTarget)) { Copy-Item -LiteralPath $rulesSource -Destination $rulesTarget }
    $sampleSource = Join-Path $ProjectRoot "workspace\inbox\sample.txt"
    $sampleTarget = Join-Path $runtimeWorkspace "inbox\sample.txt"
    if (-not (Test-Path -LiteralPath $sampleTarget)) { Copy-Item -LiteralPath $sampleSource -Destination $sampleTarget }
} finally {
    if ($null -ne $originalAttributes) { [IO.File]::SetAttributes($envPath, $originalAttributes) }
}

Set-Location -LiteralPath $ProjectRoot
docker compose config | Out-Null
if ($LASTEXITCODE -ne 0) { throw "docker compose config failed." }
Write-Host "Local Cowork setup completed."
Write-Host "Runtime Workspace: $runtimeWorkspace"
Write-Host "Existing provider keys and model settings were preserved."
