$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv was not found on PATH. Install uv and rerun this script."
}

& uv sync --frozen --all-groups --python 3.12

Write-Host ""
Write-Host "Test environment is ready at .venv"
Write-Host "Run tests with:"
Write-Host "  .\scripts\run_tests.ps1"
