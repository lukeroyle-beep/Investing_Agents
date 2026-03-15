import os
from datetime import datetime, UTC

import pandas as pd
import yfinance as yf


def fetch_market_proxy_data(ticker, name):
    try:
        data = yf.download(
            ticker,
            period="6mo",
            interval="1d",
            auto_adjust=True,
            progress=False,
        )

        if data.empty:
            print(f"Skipping {ticker} ({name}) - no data.")
            return None

        close_data = data["Close"].dropna()

        if isinstance(close_data, pd.DataFrame):
            close_series = close_data.iloc[:, 0]
        else:
            close_series = close_data

        if len(close_series) < 50:
            print(f"Skipping {ticker} ({name}) - not enough history.")
            return None

        latest_close = float(close_series.iloc[-1])
        ma50 = float(close_series.rolling(50).mean().iloc[-1])

        return {
            "ticker": ticker,
            "name": name,
            "latest_close": round(latest_close, 2),
            "ma50": round(ma50, 2),
            "above_ma50": latest_close > ma50,
        }

    except Exception as e:
        print(f"Error processing {ticker} ({name}): {e}")
        return None


def main():
    proxies = [
        {"ticker": "SPY", "name": "SPDR S&P 500 ETF Trust"},
        {"ticker": "QQQ", "name": "Invesco QQQ Trust"},
        {"ticker": "^VIX", "name": "CBOE Volatility Index"},
    ]

    results = []

    for proxy in proxies:
        ticker = proxy["ticker"]
        name = proxy["name"]

        print(f"Checking macro proxy {ticker} ({name})...")
        result = fetch_market_proxy_data(ticker, name)

        if result:
            results.append(result)

    if len(results) != 3:
        print("Macro Agent could not collect all required proxy data.")
        return

    df = pd.DataFrame(results)

    spy_row = df[df["ticker"] == "SPY"].iloc[0]
    qqq_row = df[df["ticker"] == "QQQ"].iloc[0]
    vix_row = df[df["ticker"] == "^VIX"].iloc[0]

    spy_bullish = bool(spy_row["above_ma50"])
    qqq_bullish = bool(qqq_row["above_ma50"])
    vix_value = float(vix_row["latest_close"])
    vix_bullish = vix_value < 20

    regime_score = 0
    if spy_bullish:
        regime_score += 1
    if qqq_bullish:
        regime_score += 1
    if vix_bullish:
        regime_score += 1

    if regime_score == 3:
        market_regime = "risk_on"
    elif regime_score == 2:
        market_regime = "neutral"
    else:
        market_regime = "risk_off"

    summary = pd.DataFrame(
        [
            {
                "spy_above_ma50": spy_bullish,
                "qqq_above_ma50": qqq_bullish,
                "vix_below_20": vix_bullish,
                "vix_latest_close": round(vix_value, 2),
                "regime_score": regime_score,
                "market_regime": market_regime,
                "checked_at": datetime.now(UTC).isoformat(),
            }
        ]
    )

    os.makedirs("data", exist_ok=True)

    proxy_output_path = os.path.join("data", "macro_proxies.csv")
    summary_output_path = os.path.join("data", "macro_regime.csv")

    df.to_csv(proxy_output_path, index=False)
    summary.to_csv(summary_output_path, index=False)

    print("\nMacro Agent finished.")
    print(f"Saved proxy details to: {proxy_output_path}")
    print(f"Saved regime summary to: {summary_output_path}")

    print("\nProxy details:")
    print(df)

    print("\nRegime summary:")
    print(summary)


if __name__ == "__main__":
    main()