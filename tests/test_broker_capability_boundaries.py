from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _imports_broker_package(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "brokers" or alias.name.startswith("brokers.") for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom):
            if node.module == "brokers" or str(node.module).startswith("brokers."):
                return True
    return False


def test_advisory_and_risk_agents_cannot_import_broker_capabilities():
    prohibited_roots = [
        PROJECT_ROOT / "agents" / "universe_agent",
        PROJECT_ROOT / "agents" / "macro_agent",
        PROJECT_ROOT / "agents" / "signal_agent",
        PROJECT_ROOT / "agents" / "risk_agent",
        PROJECT_ROOT / "agents" / "news_agent",
        PROJECT_ROOT / "agents" / "portfolio_agent",
        PROJECT_ROOT / "agents" / "advisory_agent",
        PROJECT_ROOT / "risk",
    ]
    violations = [
        str(path.relative_to(PROJECT_ROOT))
        for root in prohibited_roots
        for path in root.rglob("*.py")
        if _imports_broker_package(path)
    ]
    assert violations == []
