import os
from pathlib import Path

import pandas as pd

from shared.market_data import (
    MarketDataProvider,
    MarketDataResult,
    fetch_price_history,
    market_data_is_actionable,
    summarize_provider_error,
    write_market_data_health_artifact,
)
from shared.paths import (
    DATA_SOURCE_HEALTH_PATH,
    REJECTS_PATH,
    TOP_LEADS_PATH,
    UNIVERSE_SNAPSHOT_PATH,
    WATCHLIST_PATH,
    project_path,
)


UNIVERSE_FILE = project_path("data_sources", "stock_universe.csv")

REQUIRED_SOURCE_COLUMNS = [
    "ticker",
    "name",
    "asset_class",
    "region",
    "exchange",
    "source",
]
OPTIONAL_SOURCE_COLUMNS = ["index_membership", "currency", "sector", "notes"]
SOURCE_COLUMN_ORDER = REQUIRED_SOURCE_COLUMNS + OPTIONAL_SOURCE_COLUMNS

ALLOWED_ASSET_CLASSES = {
    "equity",
    "etf",
    "commodity_proxy",
    "futures",
    "fx",
    "crypto",
}


def _clean_text(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def normalise_universe_source(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise and validate the authoritative CSV universe source.

    The CSV remains the source of truth. This helper keeps the first occurrence
    for duplicate tickers after normalisation so accidental duplicate rows do
    not fan out into repeated yfinance calls or downstream candidates.
    """

    missing = [col for col in REQUIRED_SOURCE_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"Universe source missing required columns: {missing}")

    output_df = df.copy()

    for col in OPTIONAL_SOURCE_COLUMNS:
        if col not in output_df.columns:
            output_df[col] = ""

    for col in SOURCE_COLUMN_ORDER:
        output_df[col] = _clean_text(output_df[col])

    output_df["ticker"] = output_df["ticker"].str.upper()
    output_df["asset_class"] = output_df["asset_class"].str.lower()
    output_df["source"] = output_df["source"].str.lower()
    output_df["currency"] = output_df["currency"].str.upper()

    blank_required: dict[str, int] = {}
    for col in REQUIRED_SOURCE_COLUMNS:
        blank_count = int((output_df[col] == "").sum())
        if blank_count:
            blank_required[col] = blank_count

    if blank_required:
        raise ValueError(f"Universe source has blank required metadata: {blank_required}")

    invalid_asset_classes = sorted(
        set(output_df["asset_class"]) - ALLOWED_ASSET_CLASSES
    )
    if invalid_asset_classes:
        raise ValueError(
            "Universe source has invalid asset_class values: "
            f"{invalid_asset_classes}. Allowed values: {sorted(ALLOWED_ASSET_CLASSES)}"
        )

    output_df = output_df.drop_duplicates(subset=["ticker"], keep="first")
    output_df = output_df.sort_values(by=["asset_class", "region", "ticker"]).reset_index(
        drop=True
    )

    return output_df[SOURCE_COLUMN_ORDER]


def load_assets(path: str | os.PathLike[str] = UNIVERSE_FILE) -> list[dict]:
    df = pd.read_csv(Path(path), keep_default_na=False)
    df = normalise_universe_source(df)
    return df.to_dict(orient="records")


def fetch_asset_data(
    asset,
    market_data_provider: MarketDataProvider | None = None,
    health_results: list[MarketDataResult] | None = None,
):
    ticker = asset["ticker"]
    name = asset["name"]

    try:
        market_data = fetch_price_history(
            ticker,
            period="6mo",
            interval="1d",
            auto_adjust=True,
            provider=market_data_provider,
        )
        if health_results is not None:
            health_results.append(market_data)
        data = market_data.data

        if not market_data_is_actionable(market_data):
            print(
                f"Skipping {ticker} ({name}) - data is not actionable: "
                f"{market_data.metadata.reason or market_data.metadata.error}"
            )
            return None

        if data.empty:
            print(f"Skipping {ticker} ({name}) - no data.")
            return None

        close_data = data["Close"].dropna()

        if isinstance(close_data, pd.DataFrame):
            close_series = close_data.iloc[:, 0]
        else:
            close_series = close_data

        if len(close_series) < 61:
            print(f"Skipping {ticker} ({name}) - not enough history.")
            return None

        latest_close = float(close_series.iloc[-1])
        ma20 = float(close_series.rolling(20).mean().iloc[-1])
        ma50 = float(close_series.rolling(50).mean().iloc[-1])

        close_20_days_ago = float(close_series.iloc[-21])
        close_60_days_ago = float(close_series.iloc[-61])

        return_20d = ((latest_close / close_20_days_ago) - 1) * 100
        return_60d = ((latest_close / close_60_days_ago) - 1) * 100

        daily_returns = close_series.pct_change().dropna()
        volatility_20d = float(daily_returns.tail(20).std() * 100)

        above_ma20 = latest_close > ma20
        above_ma50 = latest_close > ma50
        ma20_above_ma50 = ma20 > ma50
        positive_20d_return = return_20d > 0
        positive_60d_return = return_60d > 0
        acceptable_volatility = volatility_20d < 4.0

        score = 0
        if above_ma20:
            score += 1
        if above_ma50:
            score += 1
        if ma20_above_ma50:
            score += 1
        if positive_20d_return:
            score += 1
        if positive_60d_return:
            score += 1
        if acceptable_volatility:
            score += 1

        if score >= 5:
            lead_status = "pass"
        elif score >= 2:
            lead_status = "watch"
        else:
            lead_status = "reject"

        return {
            "ticker": ticker,
            "name": name,
            "asset_class": asset["asset_class"],
            "region": asset["region"],
            "exchange": asset["exchange"],
            "source": asset["source"],
            "index_membership": asset.get("index_membership", ""),
            "currency": asset.get("currency", ""),
            "sector": asset.get("sector", ""),
            "latest_close": round(latest_close, 2),
            "ma20": round(ma20, 2),
            "ma50": round(ma50, 2),
            "return_20d_pct": round(return_20d, 2),
            "return_60d_pct": round(return_60d, 2),
            "volatility_20d_pct": round(volatility_20d, 2),
            "above_ma20": above_ma20,
            "above_ma50": above_ma50,
            "ma20_above_ma50": ma20_above_ma50,
            "positive_20d_return": positive_20d_return,
            "positive_60d_return": positive_60d_return,
            "acceptable_volatility": acceptable_volatility,
            "score": score,
            "lead_status": lead_status,
            "checked_at": market_data.metadata.fetched_at,
        }

    except Exception as e:
        raise RuntimeError(
            f"Unexpected market-data failure for {ticker}: {summarize_provider_error(e)}"
        ) from e


def main():
    assets = load_assets()
    results = []
    health_results: list[MarketDataResult] = []

    for asset in assets:
        ticker = asset["ticker"]
        name = asset["name"]

        print(f"Checking {ticker} ({name})...")
        result = fetch_asset_data(asset, health_results=health_results)

        if result:
            results.append(result)

    DATA_SOURCE_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    health_path = DATA_SOURCE_HEALTH_PATH
    write_market_data_health_artifact(health_results, health_path)

    if not results:
        print("No results collected.")
        print(f"Saved data-source health to: {health_path}")
        return

    df = pd.DataFrame(results)
    df = df.sort_values(by=["score", "ticker"], ascending=[False, True])

    snapshot_path = UNIVERSE_SNAPSHOT_PATH
    pass_path = TOP_LEADS_PATH
    watch_path = WATCHLIST_PATH
    reject_path = REJECTS_PATH

    df.to_csv(snapshot_path, index=False)
    df[df["lead_status"] == "pass"].to_csv(pass_path, index=False)
    df[df["lead_status"] == "watch"].to_csv(watch_path, index=False)
    df[df["lead_status"] == "reject"].to_csv(reject_path, index=False)

    pass_count = len(df[df["lead_status"] == "pass"])
    watch_count = len(df[df["lead_status"] == "watch"])
    reject_count = len(df[df["lead_status"] == "reject"])
    total_count = len(df)

    print("\nUniverse Agent finished.")
    print(f"Saved full snapshot to: {snapshot_path}")
    print(f"Saved top leads to: {pass_path}")
    print(f"Saved watchlist to: {watch_path}")
    print(f"Saved rejects to: {reject_path}")
    print(f"Saved data-source health to: {health_path}")

    print("\nRun summary:")
    print(f"Total assets scanned: {total_count}")
    print(f"Pass: {pass_count}")
    print(f"Watch: {watch_count}")
    print(f"Reject: {reject_count}")

    print("\nPreview:")
    print(df)


if __name__ == "__main__":
    main()
