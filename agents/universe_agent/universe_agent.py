import os
from datetime import datetime, UTC

import pandas as pd
import yfinance as yf


ASSETS = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "TSLA": "Tesla",
    "NVDA": "Nvidia",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
}


def fetch_asset_data(ticker, name):

    try:

        data = yf.download(
            ticker,
            period="6mo",
            interval="1d",
            auto_adjust=True,
            progress=False
        )

        if data.empty:
            print(f"Skipping {ticker} ({name}) — no data.")
            return None

        close_data = data["Close"].dropna()

        # Ensure we have a single column
        if isinstance(close_data, pd.DataFrame):
            close_series = close_data.iloc[:, 0]
        else:
            close_series = close_data

        latest_close = float(close_series.iloc[-1])
        ma20 = float(close_series.rolling(20).mean().iloc[-1])
        ma50 = float(close_series.rolling(50).mean().iloc[-1])

        return {
            "ticker": ticker,
            "name": name,
            "latest_close": round(latest_close, 2),
            "ma20": round(ma20, 2),
            "ma50": round(ma50, 2),
            "above_ma50": latest_close > ma50,
            "checked_at": datetime.now(UTC).isoformat()
        }

    except Exception as e:

        print(f"Error processing {ticker} ({name}): {e}")
        return None


def main():

    results = []

    for ticker, name in ASSETS.items():

        print(f"Checking {ticker} ({name})...")

        result = fetch_asset_data(ticker, name)

        if result:
            results.append(result)

    if not results:
        print("No results collected.")
        return

    df = pd.DataFrame(results)

    os.makedirs("data", exist_ok=True)

    output_path = os.path.join("data", "universe_snapshot.csv")

    df.to_csv(output_path, index=False)

    print("\nUniverse Agent finished.")
    print(f"Saved results to: {output_path}")

    print("\nPreview:")
    print(df)


if __name__ == "__main__":
    main()