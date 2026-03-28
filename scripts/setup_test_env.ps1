param(
    [string]$VenvPath = ".venv-test"
)

$ErrorActionPreference = "Stop"

if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 -m venv $VenvPath
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python -m venv $VenvPath
} else {
    throw "Python was not found on PATH. Install Python 3 and rerun this script."
}

$pythonExe = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    throw "Virtual environment creation failed. Missing interpreter at $pythonExe"
}

& $pythonExe -m pip install --upgrade pip
& $pythonExe -m pip install -r requirements-test.txt

Write-Host ""
Write-Host "Test environment is ready at $VenvPath"
Write-Host "Run tests with:"
Write-Host "  .\scripts\run_tests.ps1"
