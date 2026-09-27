param([string[]]$Models = @("gpt-oss:20b","qwen3.5:9b"))
$ErrorActionPreference = "Continue"
foreach ($model in $Models) {
    Write-Host "Testing $model ..."
    $body = @{model=$model;prompt="Reply with exactly: LOCAL_COWORK_OK";stream=$false;keep_alive="5m";options=@{num_ctx=8192;temperature=0}} | ConvertTo-Json -Depth 5
    try {
        $response = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:11434/api/generate" -ContentType "application/json" -Body $body -TimeoutSec 300
        Write-Host "$model => $($response.response.Trim())"
    } catch { Write-Warning "$model failed: $($_.Exception.Message)" }
}
