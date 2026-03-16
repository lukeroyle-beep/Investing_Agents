from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT_DIR / "agents"
DATA_DIR = ROOT_DIR / "data"
CONFIG_DIR = ROOT_DIR / "config"


def data_path(filename: str) -> Path:
    return DATA_DIR / filename


def config_path(filename: str) -> Path:
    return CONFIG_DIR / filename


def agent_path(*parts: str) -> Path:
    return AGENTS_DIR.joinpath(*parts)