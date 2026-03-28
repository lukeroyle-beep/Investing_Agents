param(
    [string]$VenvPath = ".venv-test",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = "Stop"

$pythonExe = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    throw "Missing test environment at $VenvPath. Run .\scripts\setup_test_env.ps1 first."
}

if (-not $PytestArgs -or $PytestArgs.Count -eq 0) {
    $PytestArgs = @("tests")
}

& $pythonExe -m pytest @PytestArgs
