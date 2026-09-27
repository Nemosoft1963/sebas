$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupRoot = Join-Path $ProjectRoot "backups\$stamp"
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
Compress-Archive -Path (Join-Path $ProjectRoot "workspace\*") -DestinationPath (Join-Path $backupRoot "workspace.zip") -Force
function Backup-Volume {
    param([string]$VolumeName,[string]$OutputName)
    $exists = docker volume ls --format "{{.Name}}" | Where-Object { $_ -eq $VolumeName }
    if (-not $exists) { Write-Warning "Volume not found, skipped: $VolumeName"; return }
    docker run --rm -v "${VolumeName}:/source:ro" -v "${backupRoot}:/backup" alpine sh -c "cd /source && tar czf /backup/$OutputName ."
    if ($LASTEXITCODE -ne 0) { throw "Failed to back up $VolumeName" }
}
Backup-Volume "localsaporter_app_data" "front-data.tar.gz"
Backup-Volume "localsaporter_open_webui_data" "open-webui-data.tar.gz"
Backup-Volume "localsaporter_cptr_data" "cptr-data.tar.gz"
Copy-Item "docker-compose.yml",".env.example","VERSIONS.md","BUILD_REPORT.md" $backupRoot -ErrorAction SilentlyContinue
Write-Host "Backup completed: $backupRoot"
