# Local Test Setup

Use the project environment synchronized from `uv.lock`. Python 3.12 is the
operational baseline; CI checks Python 3.11 through 3.13 on Linux and macOS.

## Create the test environment

```powershell
.\scripts\setup_test_env.ps1
```

This creates `.venv` and installs all project and development dependencies
without changing the lock.

## Run the full test suite

After activating the environment:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest -q
```

Without activating it:

```powershell
.\scripts\run_tests.ps1
```

Equivalent direct invocation without activating the environment:

```powershell
uv run --frozen python -m pytest -q
```

## Run a single test file

```powershell
.\scripts\run_tests.ps1 tests/test_invariants.py
```

## Pass through normal pytest arguments

```powershell
.\scripts\run_tests.ps1 -q
.\scripts\run_tests.ps1 tests/test_fill_agent.py -k idempotent
uv run --frozen python -m pytest tests/test_fill_agent.py -k idempotent
```

## Notes

- `pytest.ini` limits collection to the `tests/` directory.
- The tests use temp workspaces and do not rely on live price or news data.
- Install `uv` 0.11.6 before running the setup script. `uv` installs the
  project-selected Python when necessary.
- `pyproject.toml` and `uv.lock` are authoritative. `requirements.txt` is a
  generated, hash-pinned compatibility export and must not be edited manually.
- Prefer `uv run --frozen` or `.\scripts\run_tests.ps1` so tests cannot silently
  update the dependency lock.
