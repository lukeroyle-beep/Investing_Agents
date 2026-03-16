from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
import yaml

from shared.run_context import get_or_create_run_id


def ensure_parent_dir(file_path: Path) -> None:
    """
    Ensure the parent directory exists for a file path.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a copy of a DataFrame with normalised column names.
    """
    output_df = df.copy()
    output_df.columns = [
        str(col).strip().lower().replace(" ", "_").replace("-", "_")
        for col in output_df.columns
    ]
    return output_df


def safe_float(value: Any, default: float = 0.0) -> float:
    """
    Safely convert a value to float.
    """
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def read_csv_file(
    file_path: Path,
    required_columns: Optional[Iterable[str]] = None,
    empty_ok: bool = True,
    normalise: bool = False,
) -> pd.DataFrame:
    """
    General CSV reader.
    """
    if not file_path.exists():
        if empty_ok:
            return pd.DataFrame()
        raise FileNotFoundError(f"CSV file not found: {file_path}")

    df = pd.read_csv(file_path)

    if normalise:
        df = normalise_columns(df)

    if required_columns:
        missing = [col for col in required_columns if col not in df.columns]
        if missing:
            raise ValueError(
                f"Missing required columns in {file_path.name}: {missing}"
            )

    return df


def read_csv(
    file_path: Path | str,
    required_columns: Optional[Iterable[str]] = None,
    empty_ok: bool = True,
    normalise: bool = False,
) -> pd.DataFrame:
    """
    Backward-compatible CSV reader expected by older agents.
    """
    path = Path(file_path)
    return read_csv_file(
        file_path=path,
        required_columns=required_columns,
        empty_ok=empty_ok,
        normalise=normalise,
    )


def read_csv_optional(
    file_path: Path | str,
    normalise: bool = True,
) -> pd.DataFrame:
    """
    Read a CSV if it exists, otherwise return an empty DataFrame.
    """
    path = Path(file_path)
    return read_csv_file(
        file_path=path,
        empty_ok=True,
        normalise=normalise,
    )


def read_csv_required(
    file_path: Path | str,
    required_columns: Optional[Iterable[str]] = None,
    normalise: bool = True,
) -> pd.DataFrame:
    """
    Read a CSV that must exist.
    """
    path = Path(file_path)
    return read_csv_file(
        file_path=path,
        required_columns=required_columns,
        empty_ok=False,
        normalise=normalise,
    )


def write_csv_file(
    df: pd.DataFrame,
    file_path: Path,
    sort_columns: Optional[list[str]] = None,
) -> None:
    """
    Write a DataFrame to CSV.
    """
    ensure_parent_dir(file_path)

    output_df = df.copy()

    if sort_columns:
        existing_sort_columns = [c for c in sort_columns if c in output_df.columns]
        if existing_sort_columns:
            output_df = output_df.sort_values(by=existing_sort_columns)

    output_df.to_csv(file_path, index=False)


def write_csv(
    df: pd.DataFrame,
    file_path: Path | str,
    sort_columns: Optional[list[str]] = None,
) -> None:
    """
    Backward-compatible CSV writer expected by older agents.
    """
    path = Path(file_path)
    write_csv_file(df=df, file_path=path, sort_columns=sort_columns)


def add_run_id_column(
    df: pd.DataFrame,
    run_id: Optional[str] = None,
    column_name: str = "run_id",
    overwrite: bool = True,
) -> pd.DataFrame:
    """
    Add or overwrite a run_id column on a DataFrame.
    """
    output_df = df.copy()
    resolved_run_id = run_id or get_or_create_run_id()

    if overwrite or column_name not in output_df.columns:
        output_df[column_name] = resolved_run_id

    return output_df


def write_csv_with_run_id(
    df: pd.DataFrame,
    file_path: Path | str,
    sort_columns: Optional[list[str]] = None,
    run_id: Optional[str] = None,
    column_name: str = "run_id",
    overwrite: bool = True,
) -> None:
    """
    Stamp a DataFrame with run_id and write it to CSV.
    """
    path = Path(file_path)
    output_df = add_run_id_column(
        df=df,
        run_id=run_id,
        column_name=column_name,
        overwrite=overwrite,
    )
    write_csv_file(output_df, path, sort_columns=sort_columns)


def load_yaml(file_path: Path | str, empty_ok: bool = False) -> dict[str, Any]:
    """
    Load a YAML file and return a dictionary.
    """
    path = Path(file_path)

    if not path.exists():
        if empty_ok:
            return {}
        raise FileNotFoundError(f"YAML file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return data or {}


def save_yaml(data: dict[str, Any], file_path: Path | str) -> None:
    """
    Save a dictionary to a YAML file.
    """
    path = Path(file_path)
    ensure_parent_dir(path)

    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def append_csv_file(new_rows_df: pd.DataFrame, file_path: Path | str) -> pd.DataFrame:
    """
    Append rows to an existing CSV file and return the combined DataFrame.
    """
    path = Path(file_path)
    ensure_parent_dir(path)

    if path.exists():
        existing_df = pd.read_csv(path)
        combined_df = pd.concat([existing_df, new_rows_df], ignore_index=True)
    else:
        combined_df = new_rows_df.copy()

    combined_df.to_csv(path, index=False)
    return combined_df