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

After activating the test environment:

```powershell
.\.venv-test\Scripts\Activate.ps1
python -m pytest tests
```

Without activating it:

```powershell
.\scripts\run_tests.ps1
```

Equivalent direct invocation:

```powershell
.\.venv-test\Scripts\python.exe -m pytest tests
```

## Run a single test file

```powershell
.\scripts\run_tests.ps1 tests/test_invariants.py
```

## Pass through normal pytest arguments

```powershell
python -m pytest tests/test_fill_agent.py -k idempotent
.\scripts\run_tests.ps1 -q
.\scripts\run_tests.ps1 tests/test_fill_agent.py -k idempotent
.\.venv-test\Scripts\python.exe -m pytest tests/test_fill_agent.py -k idempotent
```

## Notes

- `pytest.ini` limits collection to the `tests/` directory.
- The tests use temp workspaces and do not rely on live price or news data.
- If Python is not available on `PATH`, install Python 3 first and rerun setup.
- On Windows, bare `pytest` may not be on `PATH`; prefer `python -m pytest` through `.venv-test` or `.\scripts\run_tests.ps1`.
