param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Test virtual environment not found: $python" }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $repo '.playwright-browsers'
if (-not $PytestArgs -or $PytestArgs.Count -eq 0) { $PytestArgs = @('-q', 'tests') }
Push-Location $repo
try {
    & $python -m pytest @PytestArgs
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result