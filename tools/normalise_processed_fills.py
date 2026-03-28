from __future__ import annotations

import pandas as pd

from shared.paths import PROCESSED_FILLS_PATH
from shared.schema_registry import get_file_schema
from shared.schemas import validate_processed_fills

PROCESSED_FILLS_SCHEMA = get_file_schema("processed_fills.csv")
REQUIRED_COLUMNS = PROCESSED_FILLS_SCHEMA.canonical_column_order


def main() -> None:
    path = PROCESSED_FILLS_PATH

    if not path.exists():
        df = pd.DataFrame(columns=REQUIRED_COLUMNS)
        df.to_csv(path, index=False)
        print(f"Created empty normalised ledger: {path}")
        return

    df = pd.read_csv(path)

    df = validate_processed_fills(df, keep_extra_columns=False)

    df = df[df["fill_id"] != ""].copy()
    df = df.drop_duplicates(subset=["fill_id"], keep="first")

    df.to_csv(path, index=False)
    print(f"Normalised processed fills ledger: {path}")
    print(df.tail(10).to_string(index=False))


if __name__ == "__main__":
    main()
