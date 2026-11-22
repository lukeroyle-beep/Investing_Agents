"""Mac mini host schedule: market-hours gate, secrets, lock, and LaunchAgent plist.

The LaunchAgent plist only wakes this process on a fixed interval. Whether a
pipeline run starts is decided here. ``host_schedule.enabled`` defaults to
false and is independent of broker and governance flags.

Secrets are read from the macOS Keychain or from one gitignored mode-0600 file
and are placed only in the pipeline child environment. They are not written to
logs, status, or this process's own environment.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

import yaml

from scheduler.config import SchedulerConfig
from scheduler.lock import ScheduleLockError, ScheduleRunLock
from scheduler.market_hours import NEW_YORK, MarketHoursError, assess_nyse_session
from shared.paths import (
    PROJECT_ROOT,
    RUNTIME_DIR,
    SCHEDULE_LOCK_PATH,
    SCHEDULER_CONFIG_PATH,
)


class HostScheduleConfigError(RuntimeError):
    pass


class SecretsError(RuntimeError):
    pass


OUTCOME_DISABLED = "disabled"
OUTCOME_MARKET_CLOSED = "market_closed"
OUTCOME_LOCK_HELD = "lock_held"
OUTCOME_SECRETS_ERROR = "secrets_error"
OUTCOME_PIPELINE_OK = "pipeline_ok"
OUTCOME_PIPELINE_FAILED = "pipeline_failed"

_FAILURE_OUTCOMES = {OUTCOME_SECRETS_ERROR, OUTCOME_PIPELINE_FAILED}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(\.[A-Za-z0-9][A-Za-z0-9-]*)+$")
_FORBIDDEN_ENV = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
    }
)
_TEMPLATE_PATH = Path(__file__).resolve().parent / "launchd" / "com.investing-agents.pipeline.plist.template"
_PLIST_TOKENS = ("__LABEL__", "__PYTHON__", "__REPO_ROOT__", "__INTERVAL_SECONDS__")
_SECRET_REDACTION_MIN = 4


@dataclass(frozen=True, slots=True)
class SecretBinding:
    env: str
    service: str
    account: str


@dataclass(frozen=True, slots=True)
class HostScheduleConfig:
    enabled: bool
    interval_seconds: int
    label: str
    secrets_source: str
    secrets_file: Path
    log_dir: Path
    log_max_bytes: int
    log_max_files: int
    secrets: tuple[SecretBinding, ...]
    repo_root: Path
    pipeline_script: Path
    lock_path: Path
    status_path: Path
    scheduler_config_path: Path


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class LaunchctlProbe:
    availability: str
    state: str = ""


@dataclass(frozen=True, slots=True)
class StatusReport:
    label: str
    loaded: str
    launchctl_state: str
    host_enabled: bool
    demo_preparation_enabled: bool
    broker_submission_enabled: bool
    interval_seconds: int
    last_started: str
    last_finished: str
    last_exit_code: str
    last_outcome: str
    last_detail: str
    last_error: str
    next_expected: str
    lock_status: str


Execute = Callable[[Mapping[str, str]], ProcessResult]
SecretReader = Callable[[str, str], str]
LaunchctlRunner = Callable[[str], LaunchctlProbe]


def _require_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise HostScheduleConfigError(f"host_schedule.{name} must be an integer")
    if value < minimum or value > maximum:
        raise HostScheduleConfigError(f"host_schedule.{name} is out of range")
    return value


def _resolve_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise HostScheduleConfigError(f"host_schedule.{name} must be a path")
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    parts = path.parts
    if parts and parts[0] == "runtime":
        return RUNTIME_DIR.joinpath(*parts[1:])
    return PROJECT_ROOT / path


def _bindings(raw: object) -> tuple[SecretBinding, ...]:
    if not isinstance(raw, list):
        raise HostScheduleConfigError("host_schedule.secrets must be a list")
    bindings: list[SecretBinding] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise HostScheduleConfigError("host_schedule.secrets entries must be mappings")
        env = item.get("env")
        service = item.get("service")
        account = item.get("account")
        if not isinstance(env, str) or _ENV_NAME.fullmatch(env) is None or env in _FORBIDDEN_ENV:
            raise HostScheduleConfigError("host_schedule.secrets env name is invalid")
        if env in seen:
            raise HostScheduleConfigError("host_schedule.secrets env name is duplicated")
        for field_name, field_value in (("service", service), ("account", account)):
            if (
                not isinstance(field_value, str)
                or not field_value.strip()
                or field_value != field_value.strip()
                or "\n" in field_value
                or "\r" in field_value
                or "\x00" in field_value
                or len(field_value) > 256
            ):
                raise HostScheduleConfigError(
                    f"host_schedule.secrets {field_name} is invalid"
                )
        seen.add(env)
        bindings.append(SecretBinding(env=env, service=service, account=account))
    return tuple(bindings)


def load_host_schedule(path: Path | str | None = None) -> HostScheduleConfig:
    config_path = Path(path) if path is not None else SCHEDULER_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HostScheduleConfigError("scheduler configuration is missing or invalid") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("host_schedule"), dict):
        raise HostScheduleConfigError("host_schedule section is missing")
    section: dict[str, Any] = raw["host_schedule"]
    enabled = section.get("enabled")
    if type(enabled) is not bool:
        raise HostScheduleConfigError("host_schedule.enabled must be a boolean")
    source = section.get("secrets_source")
    if source not in {"keychain", "file"}:
        raise HostScheduleConfigError("host_schedule.secrets_source must be keychain or file")
    label = section.get("label")
    if not isinstance(label, str) or _LABEL.fullmatch(label) is None:
        raise HostScheduleConfigError("host_schedule.label is invalid")
    return HostScheduleConfig(
        enabled=enabled,
        interval_seconds=_require_int(section.get("interval_seconds"), "interval_seconds", 60, 86400),
        label=label,
        secrets_source=source,
        secrets_file=_resolve_path(section.get("secrets_file"), "secrets_file"),
        log_dir=_resolve_path(section.get("log_dir"), "log_dir"),
        log_max_bytes=_require_int(section.get("log_max_bytes"), "log_max_bytes", 4096, 50_000_000),
        log_max_files=_require_int(section.get("log_max_files"), "log_max_files", 1, 1000),
        secrets=_bindings(section.get("secrets")),
        repo_root=PROJECT_ROOT,
        pipeline_script=PROJECT_ROOT / "run_pipeline.py",
        lock_path=SCHEDULE_LOCK_PATH,
        status_path=SCHEDULE_LOCK_PATH.parent / "host_schedule_status.json",
        scheduler_config_path=config_path,
    )


def redact(text: str, secrets: object) -> str:
    redacted = text
    values = secrets.values() if isinstance(secrets, Mapping) else secrets
    for secret in values:
        if isinstance(secret, str) and len(secret) >= _SECRET_REDACTION_MIN:
            redacted = redacted.replace(secret, "***")
    return redacted


def _one_line(text: str, limit: int = 240) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _assert_gitignored(path: Path, repo_root: Path) -> None:
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except (OSError, ValueError):
        return
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", relative.as_posix()],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SecretsError("git is required to confirm the secrets file is ignored") from exc
    if result.returncode != 0:
        raise SecretsError(
            "secrets file is inside the repository and is not gitignored; refusing to read it"
        )


def load_secrets_file(
    path: Path,
    bindings: tuple[SecretBinding, ...],
    *,
    repo_root: Path,
) -> dict[str, str]:
    """Read configured names from a regular file mode that excludes group and world."""
    if path.is_symlink():
        raise SecretsError("secrets file must be a regular file, not a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | nofollow)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SecretsError("secrets file must be a regular file, not a symlink") from exc
        raise SecretsError("secrets file is not readable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SecretsError("secrets file must be a regular file")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            raise SecretsError(
                f"secrets file is group or world accessible (mode {mode:04o}); refusing to read it"
            )
        _assert_gitignored(path, repo_root)
        if info.st_size > 1_048_576:
            raise SecretsError("secrets file is too large")
        try:
            raw = os.read(descriptor, info.st_size).decode("utf-8")
        except UnicodeError as exc:
            raise SecretsError("secrets file is not valid UTF-8") from exc
    finally:
        os.close(descriptor)
    parsed = _parse_env_file(raw)
    missing = [
        binding.env
        for binding in bindings
        if binding.env not in parsed or parsed[binding.env] == ""
    ]
    if missing:
        raise SecretsError("secrets file is missing values for: " + ", ".join(missing))
    return {binding.env: parsed[binding.env] for binding in bindings}


def _parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise SecretsError(f"secrets file line {line_number} is invalid")
        key, value = line.split("=", 1)
        key = key.strip()
        if _ENV_NAME.fullmatch(key) is None:
            raise SecretsError(f"secrets file line {line_number} is invalid")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if "\x00" in value:
            raise SecretsError(f"secrets file line {line_number} is invalid")
        values[key] = value
    return values


def read_keychain_password(service: str, account: str) -> str:
    """Return one generic-password item. The value is not logged."""
    command = ["security", "find-generic-password", "-s", service, "-a", account, "-w"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SecretsError("security command is not available") from exc
    if result.returncode != 0:
        raise SecretsError(
            f"keychain lookup failed for service={service} account={account} exit={result.returncode}"
        )
    value = result.stdout
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if value == "" or "\n" in value or "\x00" in value:
        raise SecretsError(
            f"keychain item was empty or unusable for service={service} account={account}"
        )
    return value


def load_keychain_secrets(
    bindings: tuple[SecretBinding, ...],
    reader: SecretReader,
) -> dict[str, str]:
    values: dict[str, str] = {}
    for binding in bindings:
        try:
            secret = reader(binding.service, binding.account)
        except SecretsError:
            raise
        except Exception as exc:
            raise SecretsError(
                f"keychain lookup failed for service={binding.service} account={binding.account}"
            ) from exc
        if not isinstance(secret, str) or secret == "" or "\n" in secret or "\x00" in secret:
            raise SecretsError(
                f"keychain item was empty or unusable for service={binding.service} account={binding.account}"
            )
        values[binding.env] = secret
    return values


def load_secrets(
    config: HostScheduleConfig,
    *,
    reader: SecretReader,
) -> dict[str, str]:
    if not config.secrets:
        return {}
    if config.secrets_source == "file":
        return load_secrets_file(config.secrets_file, config.secrets, repo_root=config.repo_root)
    if config.secrets_source == "keychain":
        return load_keychain_secrets(config.secrets, reader)
    raise SecretsError("unsupported secrets source")


def _default_execute(config: HostScheduleConfig) -> Execute:
    def execute(env: Mapping[str, str]) -> ProcessResult:
        completed = subprocess.run(
            [sys.executable, str(config.pipeline_script)],
            cwd=config.repo_root,
            env={str(key): str(value) for key, value in env.items()},
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        return ProcessResult(completed.returncode, completed.stdout, completed.stderr)

    return execute


def _cap(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    clipped = raw[:max_bytes].decode("utf-8", errors="ignore")
    return clipped + "\n... truncated ...\n"


def _append_summary(path: Path, line: str, *, max_bytes: int) -> None:
    if path.exists() and path.stat().st_size >= max_bytes:
        backup = path.with_name(path.name + ".1")
        os.replace(path, backup)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _rotate_run_logs(directory: Path, *, max_files: int) -> None:
    files = sorted(path for path in directory.glob("run-*.log") if path.is_file())
    overflow = len(files) - (max_files - 1)
    for old in files[: max(0, overflow)]:
        old.unlink(missing_ok=True)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_status(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _record_run(
    config: HostScheduleConfig,
    *,
    started: datetime,
    finished: datetime,
    outcome: str,
    detail: str,
    error: str,
    code: int,
    secrets: Mapping[str, str],
    stdout: str,
    stderr: str,
    ran_pipeline: bool,
) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.chmod(0o700)
    safe_detail = _one_line(redact(detail, secrets))
    safe_error = _one_line(redact(error, secrets))
    previous = _read_status(config.status_path)
    failure_at = str(previous.get("last_failure_at") or "")
    failure_error = str(previous.get("last_failure_error") or "")
    stored_error = safe_error if outcome in _FAILURE_OUTCOMES else ""
    if stored_error:
        failure_at = finished.isoformat()
        failure_error = stored_error
    payload = {
        "schema_version": "1.0",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "exit_code": code,
        "outcome": outcome,
        "detail": safe_detail,
        "error": stored_error,
        "last_failure_at": failure_at,
        "last_failure_error": failure_error,
    }
    _write_private(
        config.status_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    summary = (
        f"{finished.isoformat()} outcome={outcome} exit={code} "
        f"detail={safe_detail or '-'} error={stored_error or '-'}"
    )
    _append_summary(config.log_dir / "summary.log", summary, max_bytes=config.log_max_bytes)
    if not ran_pipeline:
        return
    _rotate_run_logs(config.log_dir, max_files=config.log_max_files)
    stamp = started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_log = config.log_dir / f"run-{stamp}-{os.getpid()}.log"
    body = "\n".join(
        [
            f"started_at: {started.isoformat()}",
            f"finished_at: {finished.isoformat()}",
            f"outcome: {outcome}",
            f"exit_code: {code}",
            f"detail: {safe_detail}",
            "--- stdout ---",
            _cap(redact(stdout, secrets), config.log_max_bytes),
            "--- stderr ---",
            _cap(redact(stderr, secrets), config.log_max_bytes),
            "",
        ]
    )
    _write_private(run_log, body)


def run_once(
    config: HostScheduleConfig,
    *,
    now: datetime,
    execute: Execute | None = None,
    secret_reader: SecretReader | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the pipeline when the host switch is on and the NYSE session is open.

    Disabled, closed, and lock-held results exit 0 so launchd does not treat
    them as crashes. Secret and pipeline failures exit non-zero. Broker and
    governance files are not modified.
    """
    started = now.astimezone(UTC)
    outcome = OUTCOME_PIPELINE_FAILED
    detail = "host schedule failed"
    error = ""
    code = 1
    secrets: dict[str, str] = {}
    stdout = ""
    stderr = ""
    ran_pipeline = False
    try:
        if not config.enabled:
            outcome = OUTCOME_DISABLED
            detail = "host_schedule.enabled is false"
            error = ""
            code = 0
        else:
            try:
                decision = assess_nyse_session(now)
            except MarketHoursError as exc:
                outcome = OUTCOME_PIPELINE_FAILED
                detail = "market clock was rejected"
                error = str(exc)
                code = 1
            else:
                if not decision.is_open:
                    outcome = OUTCOME_MARKET_CLOSED
                    detail = decision.reason
                    error = ""
                    code = 0
                else:
                    outcome, detail, error, code, secrets, stdout, stderr, ran_pipeline = (
                        _run_open_session(
                            config,
                            now=started,
                            execute=execute,
                            secret_reader=secret_reader or read_keychain_password,
                            environ=environ,
                        )
                    )
    except Exception as exc:
        outcome = OUTCOME_PIPELINE_FAILED
        detail = "host schedule failed"
        error = exc.__class__.__name__
        code = 1
    finished = datetime.now(UTC)
    try:
        _record_run(
            config,
            started=started,
            finished=finished,
            outcome=outcome,
            detail=detail,
            error=error,
            code=code,
            secrets=secrets,
            stdout=stdout,
            stderr=stderr,
            ran_pipeline=ran_pipeline,
        )
    except Exception:
        print(
            f"host_schedule outcome={outcome} exit={code} detail=status record failed",
            flush=True,
        )
        return code if code else 1
    safe_detail = _one_line(redact(detail, secrets)) or "-"
    print(f"host_schedule outcome={outcome} exit={code} detail={safe_detail}", flush=True)
    return code


def _run_open_session(
    config: HostScheduleConfig,
    *,
    now: datetime,
    execute: Execute | None,
    secret_reader: SecretReader,
    environ: Mapping[str, str] | None,
) -> tuple[str, str, str, int, dict[str, str], str, str, bool]:
    lock = ScheduleRunLock(config.lock_path)
    run_id = "host-" + now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        lease = lock.acquire(run_id=run_id, now=now)
    except ScheduleLockError:
        return (
            OUTCOME_LOCK_HELD,
            "overlapping run skipped",
            "",
            0,
            {},
            "",
            "",
            False,
        )
    secrets: dict[str, str] = {}
    try:
        try:
            secrets = load_secrets(config, reader=secret_reader)
        except SecretsError as exc:
            return (OUTCOME_SECRETS_ERROR, "secrets were not loaded", str(exc), 2, {}, "", "", False)
        base = os.environ if environ is None else environ
        child = {str(key): str(value) for key, value in base.items()}
        child.update(secrets)
        runner = execute if execute is not None else _default_execute(config)
        result = runner(child)
        if result.returncode == 0:
            return (
                OUTCOME_PIPELINE_OK,
                "run_pipeline.py",
                "",
                0,
                secrets,
                result.stdout,
                result.stderr,
                True,
            )
        exit_code = result.returncode if 0 < result.returncode < 126 else 1
        tail = result.stderr or result.stdout
        return (
            OUTCOME_PIPELINE_FAILED,
            f"run_pipeline.py exited {result.returncode}",
            tail,
            exit_code,
            secrets,
            result.stdout,
            result.stderr,
            True,
        )
    finally:
        lock.release(lease)


def render_plist(
    *,
    repo_root: Path,
    python_path: Path,
    label: str,
    interval_seconds: int,
) -> str:
    if type(interval_seconds) is not int or not 60 <= interval_seconds <= 86400:
        raise HostScheduleConfigError("interval_seconds is invalid")
    if _LABEL.fullmatch(label) is None:
        raise HostScheduleConfigError("host_schedule.label is invalid")
    replacements = {
        "__LABEL__": label,
        "__PYTHON__": str(python_path),
        "__REPO_ROOT__": str(repo_root),
        "__INTERVAL_SECONDS__": str(interval_seconds),
    }
    for value in replacements.values():
        if any(token in value for token in _PLIST_TOKENS):
            raise HostScheduleConfigError("plist value contains a template token")
        if any(char in value for char in "<>&\n\r"):
            raise HostScheduleConfigError("plist value contains unsupported characters")
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    for token, value in replacements.items():
        if token not in text:
            raise HostScheduleConfigError(f"plist template is missing {token}")
        text = text.replace(token, value)
    if any(token in text for token in _PLIST_TOKENS):
        raise HostScheduleConfigError("plist template was not fully rendered")
    try:
        parsed = plistlib.loads(text.encode("utf-8"))
    except (plistlib.InvalidFileException, ExpatError, ValueError) as exc:
        raise HostScheduleConfigError("rendered plist is invalid") from exc
    if parsed.get("RunAtLoad") is not False:
        raise HostScheduleConfigError("rendered plist must not run at load")
    if parsed.get("StartInterval") != interval_seconds:
        raise HostScheduleConfigError("rendered plist interval does not match")
    if not text.endswith("\n"):
        text += "\n"
    return text


def probe_launchctl(label: str) -> LaunchctlProbe:
    target = f"gui/{os.getuid()}/{label}"
    try:
        result = subprocess.run(
            ["launchctl", "print", target],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return LaunchctlProbe("unavailable")
    if result.returncode != 0:
        return LaunchctlProbe("not_loaded")
    state = ""
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("state ="):
            state = stripped.split("=", 1)[1].strip()
            break
    return LaunchctlProbe("loaded", state=state)


def _lock_status(path: Path) -> str:
    if not path.exists():
        return "free"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return "present but unreadable"
    if not isinstance(payload, dict):
        return "present but unreadable"
    pid = payload.get("pid")
    acquired = payload.get("acquired_at") or "unknown"
    run_id = payload.get("run_id") or "unknown"
    liveness = "unknown"
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            liveness = "not running"
        except PermissionError:
            liveness = "running"
        except OSError:
            liveness = "unknown"
        else:
            liveness = "running"
        return f"held pid={pid} run_id={run_id} acquired_at={acquired} ({liveness})"
    return f"held run_id={run_id} acquired_at={acquired}"


def _next_expected(finished: str, interval_seconds: int, now: datetime) -> str:
    if not finished:
        return (
            f"unknown (no run recorded; launchd interval is {interval_seconds} seconds "
            "once the agent is loaded)"
        )
    try:
        moment = datetime.fromisoformat(finished)
    except ValueError:
        return "unknown (last run time is unreadable)"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    upcoming = moment + timedelta(seconds=interval_seconds)
    clock = upcoming.astimezone(NEW_YORK)
    suffix = f" ({clock.strftime('%Y-%m-%d %H:%M %Z')})"
    if upcoming <= now:
        return upcoming.isoformat() + " (due; interval already elapsed)" + suffix
    return upcoming.isoformat() + suffix


def collect_status(
    config: HostScheduleConfig,
    *,
    now: datetime,
    launchctl: LaunchctlRunner | None = None,
) -> StatusReport:
    demo = SchedulerConfig.load(config.scheduler_config_path)
    probe = (launchctl or probe_launchctl)(config.label)
    loaded = {"loaded": "yes", "not_loaded": "no", "unavailable": "unknown"}.get(
        probe.availability, "unknown"
    )
    payload = _read_status(config.status_path)
    started = str(payload.get("started_at") or "")
    finished = str(payload.get("finished_at") or "")
    exit_code = payload.get("exit_code")
    outcome = str(payload.get("outcome") or "")
    detail = str(payload.get("detail") or "")
    failure_error = str(payload.get("last_failure_error") or "")
    failure_at = str(payload.get("last_failure_at") or "")
    if failure_error and failure_at:
        last_error = f"{failure_error} ({failure_at})"
    else:
        last_error = failure_error or "none"
    if not started and not finished:
        shown_exit = "none"
        shown_outcome = "none"
    else:
        shown_exit = str(exit_code) if exit_code is not None else "none"
        shown_outcome = outcome or "none"
    return StatusReport(
        label=config.label,
        loaded=loaded,
        launchctl_state=probe.state,
        host_enabled=config.enabled,
        demo_preparation_enabled=demo.enabled,
        broker_submission_enabled=demo.broker_submission_enabled,
        interval_seconds=config.interval_seconds,
        last_started=started,
        last_finished=finished,
        last_exit_code=shown_exit,
        last_outcome=shown_outcome,
        last_detail=detail,
        last_error=last_error,
        next_expected=_next_expected(finished, config.interval_seconds, now),
        lock_status=_lock_status(config.lock_path),
    )


def format_status(report: StatusReport) -> str:
    if report.loaded == "yes":
        state = report.launchctl_state or "unknown"
        loaded = f"yes (launchctl print state={state})"
    elif report.loaded == "no":
        loaded = "no"
    else:
        loaded = "unknown (launchctl not available)"
    outcome = report.last_outcome
    if report.last_detail and outcome not in {"", "none"}:
        outcome = f"{outcome} ({report.last_detail})"
    last_run = "never"
    if report.last_started or report.last_finished:
        last_run = f"started {report.last_started or '-'} finished {report.last_finished or '-'}"
    lines = [
        f"LaunchAgent: {report.label}",
        f"Loaded: {loaded}",
        f"Enable switch: {'on' if report.host_enabled else 'off'} (host_schedule.enabled)",
        "Scheduled Demo preparation enabled: "
        f"{str(report.demo_preparation_enabled).lower()}",
        f"Broker submission enabled: {str(report.broker_submission_enabled).lower()}",
        f"Interval: {report.interval_seconds} seconds",
        f"Last run: {last_run}",
        f"Last exit code: {report.last_exit_code}",
        f"Last outcome: {outcome}",
        f"Last error: {report.last_error}",
        f"Next expected run: {report.next_expected}",
        f"Lock: {report.lock_status}",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Render the plist. This entry point does not run the pipeline."""
    parser = argparse.ArgumentParser(
        description="Render the LaunchAgent plist. This command does not run the pipeline."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render-plist")
    render.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.command != "render-plist":
        return 2
    config = load_host_schedule(SCHEDULER_CONFIG_PATH)
    rendered = render_plist(
        repo_root=PROJECT_ROOT,
        python_path=Path(sys.executable),
        label=config.label,
        interval_seconds=config.interval_seconds,
    )
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
