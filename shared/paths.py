from __future__ import annotations

import os
import re
from pathlib import Path


# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Runtime folders
RUNTIME_DIR_ENV_VAR = "INVESTING_RUNTIME_DIR"


def resolve_runtime_dir() -> Path:
    """Resolve the untracked runtime root without creating it."""
    configured = os.environ.get(RUNTIME_DIR_ENV_VAR, "").strip()
    if not configured:
        return PROJECT_ROOT / "runtime"

    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


RUNTIME_DIR = resolve_runtime_dir()
RUNTIME_STATE_DIR = RUNTIME_DIR / "state"
RUNTIME_RUNS_DIR = RUNTIME_DIR / "runs"
RUNTIME_CONTROL_DIR = RUNTIME_DIR / "control"
RUNTIME_CACHE_DIR = RUNTIME_DIR / "cache"
RUNTIME_LOGS_DIR = RUNTIME_DIR / "logs"
RUNTIME_BACKUPS_DIR = RUNTIME_DIR / "backups"

# DATA_DIR remains as a compatibility alias while callers migrate to the
# explicit runtime-state terminology.
DATA_DIR = RUNTIME_STATE_DIR

# Core source folders
CONFIG_DIR = PROJECT_ROOT / "config"
AGENTS_DIR = PROJECT_ROOT / "agents"
SHARED_DIR = PROJECT_ROOT / "shared"
TOOLS_DIR = PROJECT_ROOT / "tools"

_SAFE_RUNTIME_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def runtime_path(*parts: str) -> Path:
    return RUNTIME_DIR.joinpath(*parts)


def state_path(*parts: str) -> Path:
    return RUNTIME_STATE_DIR.joinpath(*parts)


def run_path(run_id: str, *parts: str) -> Path:
    normalised_run_id = str(run_id).strip()
    if not _SAFE_RUNTIME_SEGMENT.fullmatch(normalised_run_id):
        raise ValueError(
            "run_id must contain only letters, numbers, dot, underscore, or hyphen"
        )
    return RUNTIME_RUNS_DIR.joinpath(normalised_run_id, *parts)


def data_path(*parts: str) -> Path:
    """Compatibility alias for state_path()."""
    return state_path(*parts)


def config_path(*parts: str) -> Path:
    if parts:
        return CONFIG_DIR.joinpath(*parts)
    return CONFIG_DIR


def agents_path(*parts: str) -> Path:
    if parts:
        return AGENTS_DIR.joinpath(*parts)
    return AGENTS_DIR


def shared_path(*parts: str) -> Path:
    if parts:
        return SHARED_DIR.joinpath(*parts)
    return SHARED_DIR


def tools_path(*parts: str) -> Path:
    if parts:
        return TOOLS_DIR.joinpath(*parts)
    return TOOLS_DIR


# Config files
SETTINGS_PATH = config_path("settings.yaml")
UNIVERSE_CONFIG_PATH = config_path("universe.yaml")
FRESHNESS_CONFIG_PATH = config_path("freshness.yaml")
EXECUTION_CONFIG_PATH = config_path("execution.yaml")
RISK_CONFIG_PATH = config_path("risk.yaml")
ETORO_DEMO_CONFIG_PATH = config_path("brokers", "etoro_demo.yaml")
ETORO_DEMO_WRITE_CONFIG_PATH = config_path("brokers", "etoro_demo_write.yaml")
EXECUTION_STORE_PATH = RUNTIME_CONTROL_DIR / "execution.sqlite3"
KILL_SWITCH_STATE_PATH = RUNTIME_CONTROL_DIR / "kill_switch.json"
KILL_SWITCH_AUDIT_PATH = RUNTIME_CONTROL_DIR / "kill_switch_audit.jsonl"
EXPERIMENT_REGISTRY_PATH = RUNTIME_CONTROL_DIR / "experiments.sqlite3"
SCHEDULER_CONFIG_PATH = CONFIG_DIR / "scheduler.yaml"
SCHEDULE_STORE_PATH = RUNTIME_CONTROL_DIR / "schedule.sqlite3"
SCHEDULE_LOCK_PATH = RUNTIME_CONTROL_DIR / "schedule.lock"

settings_path = SETTINGS_PATH
universe_config_path = UNIVERSE_CONFIG_PATH
freshness_config_path = FRESHNESS_CONFIG_PATH
execution_config_path = EXECUTION_CONFIG_PATH
risk_config_path = RISK_CONFIG_PATH

# Universe Agent outputs
UNIVERSE_SNAPSHOT_PATH = data_path("universe_snapshot.csv")
TOP_LEADS_PATH = data_path("top_leads.csv")
WATCHLIST_PATH = data_path("watchlist.csv")
REJECTS_PATH = data_path("rejects.csv")

# Macro Agent outputs
MACRO_PROXIES_PATH = data_path("macro_proxies.csv")
MACRO_REGIME_PATH = data_path("macro_regime.csv")

# Signal Agent outputs
SIGNAL_SETUPS_PATH = data_path("signal_setups.csv")
SIGNAL_TOP_SETUPS_PATH = data_path("signal_top_setups.csv")

# Risk Agent outputs
RISK_REVIEW_PATH = data_path("risk_review.csv")
RISK_APPROVED_PATH = data_path("risk_approved.csv")
RISK_CAUTION_PATH = data_path("risk_caution.csv")
RISK_VETO_PATH = data_path("risk_veto.csv")
FINAL_SHORTLIST_PATH = data_path("final_shortlist.csv")

# News Agent outputs
NEWS_REVIEW_PATH = data_path("news_review.csv")
NEWS_FLAGS_PATH = data_path("news_flags.csv")

# Portfolio Agent outputs
PORTFOLIO_CANDIDATES_PATH = data_path("portfolio_candidates.csv")
PORTFOLIO_ORDERS_PATH = data_path("portfolio_orders.csv")
PORTFOLIO_POSITIONS_PATH = data_path("portfolio_positions.csv")

# Advisory / execution / fill outputs
ADVISORY_TRADES_PATH = data_path("advisory_trades.csv")
EXECUTION_ORDERS_PATH = data_path("execution_orders.csv")
TRADE_FILLS_PATH = data_path("trade_fills.csv")
PROCESSED_FILLS_PATH = data_path("processed_fills.csv")
BROKER_FILLS_PATH = data_path("broker_fills.csv")
RUN_HISTORY_PATH = data_path("run_history.csv")
RUN_RECONCILIATION_SUMMARY_PATH = data_path("run_reconciliation_summary.csv")
DATA_SOURCE_HEALTH_PATH = data_path("data_source_health.csv")
ETORO_DEMO_READ_MODEL_PATH = data_path("broker_etoro_demo_read_model.json")

# Position / exit / journal outputs
PORTFOLIO_STATE_PATH = data_path("portfolio_state.csv")
PORTFOLIO_MONITOR_PATH = data_path("portfolio_monitor.csv")
POSITION_ALERTS_PATH = data_path("position_alerts.csv")
EXIT_ADVICE_PATH = data_path("exit_advice.csv")
JOURNAL_PATH = data_path("journal.csv")

# Lowercase aliases for direct file imports
universe_snapshot_path = UNIVERSE_SNAPSHOT_PATH
top_leads_path = TOP_LEADS_PATH
watchlist_path = WATCHLIST_PATH
rejects_path = REJECTS_PATH

macro_proxies_path = MACRO_PROXIES_PATH
macro_regime_path = MACRO_REGIME_PATH

signal_setups_path = SIGNAL_SETUPS_PATH
signal_top_setups_path = SIGNAL_TOP_SETUPS_PATH

risk_review_path = RISK_REVIEW_PATH
risk_approved_path = RISK_APPROVED_PATH
risk_caution_path = RISK_CAUTION_PATH
risk_veto_path = RISK_VETO_PATH
final_shortlist_path = FINAL_SHORTLIST_PATH

news_review_path = NEWS_REVIEW_PATH
news_flags_path = NEWS_FLAGS_PATH

portfolio_candidates_path = PORTFOLIO_CANDIDATES_PATH
portfolio_orders_path = PORTFOLIO_ORDERS_PATH
portfolio_positions_path = PORTFOLIO_POSITIONS_PATH

advisory_trades_path = ADVISORY_TRADES_PATH
execution_orders_path = EXECUTION_ORDERS_PATH
trade_fills_path = TRADE_FILLS_PATH
processed_fills_path = PROCESSED_FILLS_PATH
run_history_path = RUN_HISTORY_PATH
run_reconciliation_summary_path = RUN_RECONCILIATION_SUMMARY_PATH
data_source_health_path = DATA_SOURCE_HEALTH_PATH

portfolio_state_path = PORTFOLIO_STATE_PATH
portfolio_monitor_path = PORTFOLIO_MONITOR_PATH
position_alerts_path = POSITION_ALERTS_PATH
exit_advice_path = EXIT_ADVICE_PATH
journal_path = JOURNAL_PATH
