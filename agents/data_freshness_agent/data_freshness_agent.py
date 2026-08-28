from __future__ import annotations

from shared.freshness import assert_actionable_health
from shared.paths import DATA_SOURCE_HEALTH_PATH


AGENT_NAME = "Data Freshness Gate"


def run_data_freshness_gate() -> None:
    assert_actionable_health(DATA_SOURCE_HEALTH_PATH)
    print("Data Freshness Gate passed.")


if __name__ == "__main__":
    run_data_freshness_gate()
