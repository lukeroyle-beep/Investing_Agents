# Local Test Setup

Use the dedicated test virtual environment instead of the checked-in `.venv`.
The checked-in environment is machine-specific and is not a reliable local test runner.

## Create the test environment

```powershell
.\scripts\setup_test_env.ps1
```

This creates `.venv-test` and installs:

- project dependencies from `requirements.txt`
- test dependency `pytest` from `requirements-test.txt`

## Run the full test suite

```powershell
.\scripts\run_tests.ps1
```

## Run a single test file

```powershell
.\scripts\run_tests.ps1 tests/test_invariants.py
```

## Pass through normal pytest arguments

```powershell
.\scripts\run_tests.ps1 -q
.\scripts\run_tests.ps1 tests/test_fill_agent.py -k idempotent
```

## Notes

- `pytest.ini` limits collection to the `tests/` directory.
- The tests use temp workspaces and do not rely on live price or news data.
- If Python is not available on `PATH`, install Python 3 first and rerun setup.
