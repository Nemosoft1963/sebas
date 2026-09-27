$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot
$python=Join-Path $root '.venv\Scripts\python.exe'
$args=@('-m','uvicorn','app.mic_bridge:app','--app-dir',$root,'--host','127.0.0.1','--port','8098')
Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $env:TEMP -WindowStyle Hidden
Write-Host 'Mic bridge: http://127.0.0.1:8098'
