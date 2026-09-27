$ErrorActionPreference = 'Stop'
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m app.main --check
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
  nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv
}
