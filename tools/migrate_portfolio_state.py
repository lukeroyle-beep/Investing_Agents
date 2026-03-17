from __future__ import annotations

import pandas as pd

from shared.paths import PORTFOLIO_STATE_PATH
from shared.schemas import validate_portfolio_state


def main() -> None:
    path = PORTFOLIO_STATE_PATH

    if not path.exists():
        empty_df = validate_portfolio_state(pd.DataFrame(), keep_extra_columns=False)
        empty_df.to_csv(path, index=False)
        print(f"Created empty canonical portfolio state: {path}")
        return

    raw_df = pd.read_csv(path)
    migrated_df = validate_portfolio_state(raw_df, keep_extra_columns=False)
    migrated_df.to_csv(path, index=False)

    print(f"Migrated portfolio state to canonical schema: {path}")
    print()
    print("Preview:")
    print(migrated_df.head(10).to_string(index=False))


if __name__ == "__main__":
    main()