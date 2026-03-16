from __future__ import annotations

import pandas as pd
from pathlib import Path

from shared.paths import PROCESSED_FILLS_PATH

REQUIRED_COLUMNS = ["fill_id", "processed_at", "run_id"]


def main() -> None:
    path = PROCESSED_FILLS_PATH

    if not path.exists():
        df = pd.DataFrame(columns=REQUIRED_COLUMNS)
        df.to_csv(path, index=False)
        print(f"Created empty normalised ledger: {path}")
        return

    df = pd.read_csv(path)

    if "fill_id" not in df.columns:
        df["fill_id"] = ""

    if "processed_at" not in df.columns:
        df["processed_at"] = ""

    if "run_id" not in df.columns:
        df["run_id"] = ""

    df = df[REQUIRED_COLUMNS].copy()
    df["fill_id"] = df["fill_id"].fillna("").astype(str).str.strip()
    df["processed_at"] = df["processed_at"].fillna("").astype(str).str.strip()
    df["run_id"] = df["run_id"].fillna("").astype(str).str.strip()

    df = df[df["fill_id"] != ""].copy()
    df = df.drop_duplicates(subset=["fill_id"], keep="first")

    df.to_csv(path, index=False)
    print(f"Normalised processed fills ledger: {path}")
    print(df.tail(10).to_string(index=False))


if __name__ == "__main__":
    main()