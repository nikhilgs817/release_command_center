$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$pythonExe = Join-Path $appRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    python -m venv (Join-Path $appRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}
& $pythonExe -m pip install -r (Join-Path $appRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install application dependencies.' }
Set-Location -LiteralPath $appRoot
$env:DEMO_MODE = 'true'
$env:ALLOW_EXTERNAL_WRITES = 'false'
$env:DATABASE_URL = ''
Write-Host 'Release Command Center: http://127.0.0.1:8000'
& $pythonExe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
