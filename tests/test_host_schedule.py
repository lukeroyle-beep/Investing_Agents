from __future__ import annotations

import json
import os
import plistlib
import shutil
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from scheduler.config import SchedulerConfig
from scheduler.host_schedule import (
    HostScheduleConfig,
    HostScheduleConfigError,
    LaunchctlProbe,
    ProcessResult,
    SecretBinding,
    SecretsError,
    StatusReport,
    collect_status,
    format_status,
    _assert_gitignored,
    load_host_schedule,
    load_secrets_file,
    read_keychain_password,
    render_plist,
    run_once,
)
from scheduler.lock import ScheduleRunLock
from scheduler.market_hours import (
    REASON_AFTER_CLOSE,
    REASON_BEFORE_OPEN,
    REASON_EARLY_CLOSE,
    REASON_HOLIDAY,
    REASON_OPEN,
    REASON_WEEKEND,
    MarketHoursError,
    assess_nyse_session,
)
from shared.freshness import ExchangeCalendar
from shared.paths import PROJECT_ROOT, SCHEDULER_CONFIG_PATH


NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
SECRET = "SYNTHETIC_SECRET_VALUE_DO_NOT_LOG"
ENV_NAME = "REPLACE_WITH_BROKER_ENV_VAR"
OPEN_SESSION = datetime(2026, 7, 8, 14, 0, tzinfo=NY)


def _ny(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY)


def _london_fixed_window_open(moment: datetime) -> bool:
    """Incorrect gate: 14:30-21:00 Europe/London.

    That window matches 09:30-16:00 New York only while the civil offset is
    five hours. It is wrong during the weeks when the US and UK disagree.
    """
    local = moment.astimezone(LONDON)
    start = local.replace(hour=14, minute=30, second=0, microsecond=0)
    end = local.replace(hour=21, minute=0, second=0, microsecond=0)
    return start <= local < end


def _offset_hours(moment: datetime) -> float:
    london = moment.astimezone(LONDON).utcoffset()
    new_york = moment.astimezone(NY).utcoffset()
    assert london is not None and new_york is not None
    return (london - new_york).total_seconds() / 3600


def _binding() -> SecretBinding:
    return SecretBinding(env=ENV_NAME, service="investing-agents-demo", account="api")


def _config(tmp_path: Path, **overrides: object) -> HostScheduleConfig:
    values: dict[str, object] = {
        "enabled": True,
        "interval_seconds": 900,
        "label": "com.investing-agents.pipeline",
        "secrets_source": "keychain",
        "secrets_file": tmp_path / "secrets.env",
        "log_dir": tmp_path / "logs",
        "log_max_bytes": 20000,
        "log_max_files": 3,
        "secrets": (),
        "repo_root": PROJECT_ROOT,
        "pipeline_script": tmp_path / "run_pipeline.py",
        "lock_path": tmp_path / "control" / "schedule.lock",
        "status_path": tmp_path / "control" / "host_schedule_status.json",
        "scheduler_config_path": SCHEDULER_CONFIG_PATH,
    }
    values.update(overrides)
    return HostScheduleConfig(**values)  # type: ignore[arg-type]


def _tracked_bytes() -> dict[str, bytes]:
    relative = (
        "config/scheduler.yaml",
        "config/execution.yaml",
        "config/governance.yaml",
        "config/brokers/etoro_demo.yaml",
        "config/brokers/etoro_demo_write.yaml",
    )
    return {name: (PROJECT_ROOT / name).read_bytes() for name in relative}


def test_checked_in_host_schedule_is_disabled_and_does_not_flip_broker_flags():
    raw = yaml.safe_load(SCHEDULER_CONFIG_PATH.read_text(encoding="utf-8"))
    config = load_host_schedule(SCHEDULER_CONFIG_PATH)
    demo = SchedulerConfig.load(SCHEDULER_CONFIG_PATH)
    assert raw["enabled"] is False
    assert raw["broker_submission_enabled"] is False
    assert raw["host_schedule"]["enabled"] is False
    assert raw["host_schedule"]["secrets"] == []
    assert config.enabled is False
    assert config.interval_seconds == 900
    assert config.secrets == ()
    assert demo.enabled is False
    assert demo.broker_submission_enabled is False


def test_dst_transition_weeks_follow_new_york_not_london():
    spring_open = _ny(2026, 3, 11, 9, 30)
    spring_closed = _ny(2026, 3, 11, 16, 30)
    autumn_open = _ny(2026, 10, 26, 9, 30)
    autumn_closed = _ny(2026, 10, 26, 16, 30)
    summer_open = _ny(2026, 7, 8, 9, 30)
    summer_closed = _ny(2026, 7, 8, 16, 30)

    assert _offset_hours(spring_open) == 4
    assert _offset_hours(autumn_open) == 4
    assert _offset_hours(summer_open) == 5
    assert spring_open.astimezone(LONDON).hour == 13
    assert autumn_open.astimezone(LONDON).hour == 13
    assert summer_open.astimezone(LONDON).hour == 14

    for moment in (spring_open, autumn_open, summer_open):
        decision = assess_nyse_session(moment)
        assert decision.is_open is True
        assert decision.reason == REASON_OPEN
        assert assess_nyse_session(moment.astimezone(LONDON)).is_open is True

    for moment in (spring_closed, autumn_closed, summer_closed):
        decision = assess_nyse_session(moment)
        assert decision.is_open is False
        assert decision.reason == REASON_AFTER_CLOSE

    assert _london_fixed_window_open(spring_open) is False
    assert _london_fixed_window_open(autumn_open) is False
    assert _london_fixed_window_open(spring_closed) is True
    assert _london_fixed_window_open(autumn_closed) is True
    assert _london_fixed_window_open(summer_open) is True
    assert _london_fixed_window_open(summer_closed) is False


def test_nyse_holiday_early_close_and_weekend():
    holiday = assess_nyse_session(_ny(2026, 11, 26, 10, 0))
    assert holiday.is_open is False
    assert holiday.reason == REASON_HOLIDAY
    assert _london_fixed_window_open(_ny(2026, 11, 26, 10, 0)) is True

    observed = assess_nyse_session(_ny(2026, 7, 3, 10, 0))
    assert observed.reason == REASON_HOLIDAY

    before_early_close = assess_nyse_session(_ny(2026, 11, 27, 12, 30))
    at_early_close = assess_nyse_session(_ny(2026, 11, 27, 13, 0))
    afternoon = assess_nyse_session(_ny(2026, 11, 27, 15, 0))
    assert before_early_close.is_open is True
    assert at_early_close.is_open is False
    assert at_early_close.reason == REASON_EARLY_CLOSE
    assert afternoon.reason == REASON_EARLY_CLOSE
    assert _london_fixed_window_open(_ny(2026, 11, 27, 15, 0)) is True

    christmas_eve = assess_nyse_session(_ny(2026, 12, 24, 13, 0))
    assert christmas_eve.reason == REASON_EARLY_CLOSE

    assert assess_nyse_session(_ny(2026, 11, 28, 10, 0)).reason == REASON_WEEKEND
    assert assess_nyse_session(_ny(2026, 11, 29, 10, 0)).reason == REASON_WEEKEND


def test_regular_session_bounds_and_rejected_clocks():
    assert assess_nyse_session(_ny(2026, 7, 8, 9, 29)).reason == REASON_BEFORE_OPEN
    opened = assess_nyse_session(_ny(2026, 7, 8, 9, 30))
    assert opened.is_open is True
    assert assess_nyse_session(_ny(2026, 7, 8, 15, 59)).is_open is True
    assert assess_nyse_session(_ny(2026, 7, 8, 16, 0)).reason == REASON_AFTER_CLOSE
    with pytest.raises(MarketHoursError):
        assess_nyse_session(datetime(2026, 7, 8, 14, 0))
    with pytest.raises(MarketHoursError):
        assess_nyse_session(OPEN_SESSION, ExchangeCalendar(timezone="Europe/London"))


def test_2026_gate_matches_reviewed_session_snapshot():
    raw = yaml.safe_load(
        (PROJECT_ROOT / "config/market_sessions/nasdaq_2026.yaml").read_text(encoding="utf-8")
    )
    for item in raw["full_holidays"]:
        decision = assess_nyse_session(datetime.fromisoformat(f"{item}T10:00:00").replace(tzinfo=NY))
        assert decision.reason == REASON_HOLIDAY
    for day, close in raw["early_closes"].items():
        hour, minute = (int(part) for part in close.split(":"))
        during = assess_nyse_session(datetime.fromisoformat(f"{day}T12:00:00").replace(tzinfo=NY))
        at_close = assess_nyse_session(
            datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=NY)
        )
        assert during.is_open is True
        assert at_close.reason == REASON_EARLY_CLOSE


def test_enable_switch_off_exits_without_pipeline_or_secrets(tmp_path: Path):
    secret_file = tmp_path / "secrets.env"
    secret_file.write_text(f"{ENV_NAME}={SECRET}\n", encoding="utf-8")
    secret_file.chmod(0o644)
    calls: list[dict[str, str]] = []

    def execute(env):
        calls.append(dict(env))
        return ProcessResult(0, SECRET, "")

    config = _config(
        tmp_path,
        enabled=False,
        secrets_source="file",
        secrets=( _binding(), ),
        secrets_file=secret_file,
    )
    code = run_once(config, now=OPEN_SESSION, execute=execute, environ={})
    text = _written(tmp_path)
    assert code == 0
    assert calls == []
    assert "outcome=disabled" in text
    assert SECRET not in text
    assert not config.lock_path.exists()


def test_market_closed_skips_pipeline(tmp_path: Path):
    calls: list[object] = []
    config = _config(tmp_path)
    code = run_once(
        config,
        now=_ny(2026, 11, 28, 10, 0),
        execute=lambda env: calls.append(env) or ProcessResult(0, "", ""),
        environ={},
    )
    assert code == 0
    assert calls == []
    assert "outcome=market_closed" in _written(tmp_path)
    assert "weekend" in _written(tmp_path)


def test_lock_blocks_overlapping_run_and_is_released(tmp_path: Path):
    config = _config(tmp_path)
    lock = ScheduleRunLock(config.lock_path)
    lease = lock.acquire(run_id="manual-run", now=OPEN_SESSION)
    calls: list[object] = []
    code = run_once(
        config,
        now=OPEN_SESSION,
        execute=lambda env: calls.append(env) or ProcessResult(0, "", ""),
        environ={},
    )
    assert code == 0
    assert calls == []
    assert config.lock_path.exists()
    assert "outcome=lock_held" in _written(tmp_path)
    lock.release(lease)

    seen: dict[str, bool] = {}

    def execute(env):
        seen["locked"] = config.lock_path.exists()
        return ProcessResult(0, "", "")

    code = run_once(config, now=OPEN_SESSION, execute=execute, environ={})
    assert code == 0
    assert seen["locked"] is True
    assert not config.lock_path.exists()
    assert "outcome=pipeline_ok" in _written(tmp_path)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666, 0o607])
def test_secrets_loader_refuses_group_or_world_access(tmp_path: Path, mode: int):
    path = tmp_path / "launchd.env"
    path.write_text(f"{ENV_NAME}={SECRET}\n", encoding="utf-8")
    path.chmod(mode)
    with pytest.raises(SecretsError, match="group or world") as caught:
        load_secrets_file(path, (_binding(),), repo_root=PROJECT_ROOT)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_secrets_loader_accepts_owner_only_file(tmp_path: Path, mode: int):
    path = tmp_path / "launchd.env"
    path.write_text(f"{ENV_NAME}={SECRET}\n", encoding="utf-8")
    path.chmod(mode)
    loaded = load_secrets_file(path, (_binding(),), repo_root=PROJECT_ROOT)
    assert loaded == {ENV_NAME: SECRET}


def test_secrets_loader_refuses_symlink_and_unignored_repo_file(tmp_path: Path):
    target = tmp_path / "real.env"
    target.write_text(f"{ENV_NAME}={SECRET}\n", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "linked.env"
    link.symlink_to(target)
    with pytest.raises(SecretsError, match="symlink"):
        load_secrets_file(link, (_binding(),), repo_root=PROJECT_ROOT)

    leaked = PROJECT_ROOT / "scheduler" / "launchd" / "_not_ignored.env"
    leaked.write_text(f"{ENV_NAME}={SECRET}\n", encoding="utf-8")
    leaked.chmod(0o600)
    try:
        with pytest.raises(SecretsError, match="not gitignored") as caught:
            load_secrets_file(leaked, (_binding(),), repo_root=PROJECT_ROOT)
        assert SECRET not in str(caught.value)
    finally:
        leaked.unlink(missing_ok=True)


def test_default_secrets_path_is_gitignored():
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", "runtime/secrets/launchd.env"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    tracked = subprocess.run(
        ["git", "check-ignore", "-q", "--", "README.md"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    assert ignored.returncode == 0
    assert tracked.returncode == 1
    _assert_gitignored(PROJECT_ROOT / "runtime" / "secrets" / "launchd.env", PROJECT_ROOT)


def test_wrapper_exports_file_secret_only_to_child_and_redacts_logs(tmp_path: Path):
    path = tmp_path / "launchd.env"
    path.write_text(f"# comment\nexport {ENV_NAME}={SECRET}\n", encoding="utf-8")
    path.chmod(0o600)
    seen: dict[str, str] = {}

    def execute(env):
        seen.update(env)
        return ProcessResult(0, f"pipeline said {SECRET}\n", "")

    before = os.environ.get(ENV_NAME)
    config = _config(
        tmp_path,
        secrets_source="file",
        secrets=(_binding(),),
        secrets_file=path,
    )
    gates = _tracked_bytes()
    code = run_once(config, now=OPEN_SESSION, execute=execute, environ={"SAFE": "1"})
    assert code == 0
    assert seen == {"SAFE": "1", ENV_NAME: SECRET}
    assert os.environ.get(ENV_NAME) == before
    written = _written(tmp_path)
    assert SECRET not in written
    assert "***" in written
    assert _tracked_bytes() == gates
    run_logs = list((tmp_path / "logs").glob("run-*.log"))
    assert len(run_logs) == 1
    assert stat.S_IMODE(run_logs[0].stat().st_mode) & 0o077 == 0


def test_world_readable_file_skips_pipeline(tmp_path: Path):
    path = tmp_path / "launchd.env"
    path.write_text(f"{ENV_NAME}={SECRET}\n", encoding="utf-8")
    path.chmod(0o644)
    calls: list[object] = []
    config = _config(
        tmp_path,
        secrets_source="file",
        secrets=(_binding(),),
        secrets_file=path,
    )
    code = run_once(
        config,
        now=OPEN_SESSION,
        execute=lambda env: calls.append(env) or ProcessResult(0, "", ""),
        environ={},
    )
    assert code == 2
    assert calls == []
    text = _written(tmp_path)
    assert "outcome=secrets_error" in text
    assert SECRET not in text


def test_keychain_reader_uses_security_and_does_not_log_the_password(monkeypatch, tmp_path: Path):
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        assert kwargs.get("capture_output") is True
        return subprocess.CompletedProcess(command, 0, stdout=SECRET + "\n", stderr="hidden")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert read_keychain_password("investing-agents-demo", "api") == SECRET
    assert calls == [
        ["security", "find-generic-password", "-s", "investing-agents-demo", "-a", "api", "-w"]
    ]

    seen: dict[str, str] = {}
    config = _config(tmp_path, secrets=(_binding(),))
    code = run_once(
        config,
        now=OPEN_SESSION,
        execute=lambda env: seen.update(env) or ProcessResult(7, "", SECRET),
        secret_reader=lambda service, account: SECRET,
        environ={},
    )
    assert code == 7
    assert seen[ENV_NAME] == SECRET
    assert SECRET not in _written(tmp_path)
    assert "outcome=pipeline_failed" in _written(tmp_path)


def test_rendered_plist_is_valid_and_does_not_run_at_load(tmp_path: Path):
    rendered = render_plist(
        repo_root=tmp_path,
        python_path=tmp_path / ".venv" / "bin" / "python",
        label="com.investing-agents.pipeline",
        interval_seconds=900,
    )
    parsed = plistlib.loads(rendered.encode("utf-8"))
    assert parsed["Label"] == "com.investing-agents.pipeline"
    assert parsed["RunAtLoad"] is False
    assert parsed["StartInterval"] == 900
    assert parsed["ProgramArguments"][1].endswith("scripts/run_host_schedule.py")
    assert "StandardOutPath" in parsed and "StandardErrorPath" in parsed
    assert SECRET not in rendered
    assert "__" not in rendered
    destination = tmp_path / "agent.plist"
    destination.write_text(rendered, encoding="utf-8")
    plutil = shutil.which("plutil")
    if plutil:
        completed = subprocess.run(
            [plutil, "-lint", str(destination)],
            check=False,
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr
    plistutil = shutil.which("plistutil")
    if plistutil:
        completed = subprocess.run(
            [plistutil, "-i", str(destination), "-f", "xml", "-o", str(tmp_path / "agent-xml.plist")],
            check=False,
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr


def test_status_reports_switch_last_run_and_next_fire(tmp_path: Path):
    config = _config(tmp_path, enabled=False)
    run_once(
        config,
        now=OPEN_SESSION,
        execute=lambda env: ProcessResult(0, "", ""),
        environ={},
    )
    report = collect_status(
        config,
        now=datetime(2026, 7, 8, 18, 5, tzinfo=NY),
        launchctl=lambda label: LaunchctlProbe("not_loaded"),
    )
    text = format_status(report)
    assert "Loaded: no" in text
    assert "Enable switch: off" in text
    assert "Scheduled Demo preparation enabled: false" in text
    assert "Broker submission enabled: false" in text
    assert "Last outcome: disabled" in text
    assert "Last exit code: 0" in text
    assert "Last error: none" in text
    assert "Next expected run:" in text
    assert "EDT" in text or "EST" in text
    assert "Lock: free" in text

    unavailable = format_status(
        StatusReport(
            label=config.label,
            loaded="unknown",
            launchctl_state="",
            host_enabled=False,
            demo_preparation_enabled=False,
            broker_submission_enabled=False,
            interval_seconds=900,
            last_started="",
            last_finished="",
            last_exit_code="none",
            last_outcome="none",
            last_detail="",
            last_error="none",
            next_expected="unknown (no run recorded)",
            lock_status="free",
        )
    )
    assert "unknown (launchctl not available)" in unavailable


def test_run_logs_rotate_to_the_configured_cap(tmp_path: Path):
    config = _config(tmp_path, log_max_files=2)
    for minute in (0, 1, 2):
        run_once(
            config,
            now=_ny(2026, 7, 8, 14, minute),
            execute=lambda env: ProcessResult(0, "ok", ""),
            environ={},
        )
    assert len(list((tmp_path / "logs").glob("run-*.log"))) == 2


def test_shell_scripts_use_bootout_and_do_not_call_deprecated_load():
    install = (PROJECT_ROOT / "scripts" / "install_launch_agent.sh").read_text(encoding="utf-8")
    uninstall = (PROJECT_ROOT / "scripts" / "uninstall_launch_agent.sh").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    for script in (install, uninstall):
        assert "launchctl load" not in script
        assert "launchctl unload" not in script
        assert "launchctl bootout" in script
    assert "launchctl bootstrap" in install
    assert "launchctl bootstrap" in readme
    for name in ("install_launch_agent.sh", "uninstall_launch_agent.sh"):
        completed = subprocess.run(
            ["bash", "-n", str(PROJECT_ROOT / "scripts" / name)],
            check=False,
            text=True,
        )
        assert completed.returncode == 0


def test_install_does_not_bootstrap_and_uninstall_removes_plist(tmp_path: Path):
    python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not python.is_file():
        pytest.skip("project virtualenv is required to render the plist")
    home = tmp_path / "home"
    bindir = tmp_path / "bin"
    home.mkdir()
    bindir.mkdir()
    log = tmp_path / "launchctl.log"
    fake = bindir / "launchctl"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\nexit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["LAUNCHCTL_LOG"] = str(log)
    installed = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "install_launch_agent.sh")],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    plist_path = home / "Library" / "LaunchAgents" / "com.investing-agents.pipeline.plist"
    assert plist_path.is_file()
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["RunAtLoad"] is False
    assert parsed["StartInterval"] == 900
    recorded = log.read_text(encoding="utf-8")
    assert "bootout " in recorded
    assert "bootstrap" not in recorded
    assert "not loaded" in installed.stdout
    removed = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "uninstall_launch_agent.sh")],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert removed.returncode == 0, removed.stderr
    assert not plist_path.exists()
    assert "config/scheduler.yaml was not modified" in removed.stdout
    assert yaml.safe_load(SCHEDULER_CONFIG_PATH.read_text(encoding="utf-8"))["host_schedule"]["enabled"] is False


def test_invalid_enabled_value_is_rejected(tmp_path: Path):
    source = yaml.safe_load(SCHEDULER_CONFIG_PATH.read_text(encoding="utf-8"))
    source["host_schedule"]["enabled"] = "false"
    path = tmp_path / "scheduler.yaml"
    path.write_text(yaml.safe_dump(source), encoding="utf-8")
    with pytest.raises(HostScheduleConfigError, match="boolean"):
        load_host_schedule(path)


def _written(tmp_path: Path) -> str:
    parts: list[str] = []
    for root in (tmp_path / "logs", tmp_path / "control"):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)
