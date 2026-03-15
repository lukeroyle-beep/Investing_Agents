import os
from datetime import datetime, UTC

import pandas as pd
import yfinance as yf


TOP_LEADS_FILE = os.path.join("data", "top_leads.csv")
WATCHLIST_FILE = os.path.join("data", "watchlist.csv")
MACRO_REGIME_FILE = os.path.join("data", "macro_regime.csv")


def load_candidate_assets():
    
    frames = []

    if os.path.exists(TOP_LEADS_FILE):
        frames.append(pd.read_csv(TOP_LEADS_FILE))

    if os.path.exists(WATCHLIST_FILE):
        frames.append(pd.read_csv(WATCHLIST_FILE))

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)

    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["name"] = df["name"].astype(str).str.strip()

    df = df.drop_duplicates(subset=["ticker"], keep="first")

    return df[["ticker", "name"]]

def load_market_regime():
    if not os.path.exists(MACRO_REGIME_FILE):
        return "neutral"

    df = pd.read_csv(MACRO_REGIME_FILE)

    if df.empty or "market_regime" not in df.columns:
        return "neutral"

    return str(df.iloc[0]["market_regime"]).strip().lower()

def calculate_rsi(close_series, period=14):
    delta = close_series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return float(rsi.iloc[-1])


def calculate_atr(high_series, low_series, close_series, period=14):
    prev_close = close_series.shift(1)

    tr1 = high_series - low_series
    tr2 = (high_series - prev_close).abs()
    tr3 = (low_series - prev_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.rolling(period).mean()

    return float(atr.iloc[-1])


def fetch_signal_data(ticker, name):
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
        high_data = data["High"].dropna()
        low_data = data["Low"].dropna()
        volume_data = data["Volume"].dropna()

        if isinstance(close_data, pd.DataFrame):
            close_series = close_data.iloc[:, 0]
        else:
            close_series = close_data

        if isinstance(high_data, pd.DataFrame):
            high_series = high_data.iloc[:, 0]
        else:
            high_series = high_data

        if isinstance(low_data, pd.DataFrame):
            low_series = low_data.iloc[:, 0]
        else:
            low_series = low_data

        if isinstance(volume_data, pd.DataFrame):
            volume_series = volume_data.iloc[:, 0]
        else:
            volume_series = volume_data

        if len(close_series) < 50 or len(volume_series) < 20:
            print(f"Skipping {ticker} ({name}) - not enough history.")
            return None

        latest_close = float(close_series.iloc[-1])
        ma20 = float(close_series.rolling(20).mean().iloc[-1])

        high_20 = float(close_series.tail(20).max())
        high_50 = float(close_series.tail(50).max())

        latest_volume = float(volume_series.iloc[-1])
        avg_volume_20 = float(volume_series.tail(20).mean())
        volume_ratio = latest_volume / avg_volume_20 if avg_volume_20 > 0 else 0

        rsi_14 = calculate_rsi(close_series, period=14)
        atr_14 = calculate_atr(high_series, low_series, close_series, period=14)
        atr_pct = (atr_14 / latest_close) * 100 if latest_close > 0 else 0

        within_2pct_of_20d_high = latest_close >= high_20 * 0.98
        within_5pct_of_50d_high = latest_close >= high_50 * 0.95
        above_ma20 = latest_close > ma20
        elevated_volume = volume_ratio > 1.2

        rsi_bullish = rsi_14 >= 55
        atr_reasonable = atr_pct <= 5.0

        confirmed_breakout = latest_close >= high_20 and elevated_volume
        breakout_ready = within_2pct_of_20d_high and above_ma20
        pre_breakout = within_5pct_of_50d_high and above_ma20

        if confirmed_breakout:
            breakout_type = "confirmed_breakout"
        elif breakout_ready:
            breakout_type = "breakout_ready"
        elif pre_breakout:
            breakout_type = "pre_breakout"
        else:
            breakout_type = "weak"

        setup_score = 0
        if within_2pct_of_20d_high:
            setup_score += 1
        if within_5pct_of_50d_high:
            setup_score += 1
        if above_ma20:
            setup_score += 1
        if elevated_volume:
            setup_score += 1
        if rsi_bullish:
            setup_score += 1
        if atr_reasonable:
            setup_score += 1

        if setup_score >= 5:
            setup_status = "actionable"
        elif setup_score >= 3:
            setup_status = "watch"
        else:
            setup_status = "weak"

        return {
            "ticker": ticker,
            "name": name,
            "latest_close": round(latest_close, 2),
            "ma20": round(ma20, 2),
            "high_20": round(high_20, 2),
            "high_50": round(high_50, 2),
            "latest_volume": int(latest_volume),
            "avg_volume_20": int(avg_volume_20),
            "volume_ratio": round(volume_ratio, 2),
            "rsi_14": round(rsi_14, 2),
            "atr_14": round(atr_14, 2),
            "atr_pct": round(atr_pct, 2),
            "within_2pct_of_20d_high": within_2pct_of_20d_high,
            "within_5pct_of_50d_high": within_5pct_of_50d_high,
            "above_ma20": above_ma20,
            "elevated_volume": elevated_volume,
            "rsi_bullish": rsi_bullish,
            "atr_reasonable": atr_reasonable,
            "confirmed_breakout": confirmed_breakout,
            "breakout_ready": breakout_ready,
            "pre_breakout": pre_breakout,
            "breakout_type": breakout_type,
            "setup_score": setup_score,
            "setup_status": setup_status,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    except Exception as e:
        print(f"Error processing {ticker} ({name}): {e}")
        return None


def main():
    candidates = load_candidate_assets()

    market_regime = load_market_regime()

    if candidates.empty:
        print("No candidate assets found. Run Universe Agent first.")
        return

    results = []

    for _, row in candidates.iterrows():
        ticker = row["ticker"]
        name = row["name"]

        print(f"Checking signal setup for {ticker} ({name})...")
        result = fetch_signal_data(ticker, name)

        if result:
            adjusted_score = result["setup_score"]

            if market_regime == "risk_off":
                adjusted_score = max(0, adjusted_score - 1)

            result["market_regime"] = market_regime
            result["adjusted_setup_score"] = adjusted_score

            if adjusted_score >= 5:
                result["adjusted_setup_status"] = "actionable"
            elif adjusted_score >= 3:
                result["adjusted_setup_status"] = "watch"
            else:
                result["adjusted_setup_status"] = "weak"

            results.append(result)

    if not results:
        print("No signal results collected.")
        return

    df = pd.DataFrame(results)
    df = df.sort_values(by=["adjusted_setup_score", "ticker"], ascending=[False, True])

    os.makedirs("data", exist_ok=True)

    all_setups_path = os.path.join("data", "signal_setups.csv")
    top_setups_path = os.path.join("data", "signal_top_setups.csv")

    df.to_csv(all_setups_path, index=False)
    df[df["adjusted_setup_status"] == "actionable"].to_csv(top_setups_path, index=False)

    actionable_count = len(df[df["adjusted_setup_status"] == "actionable"])
    watch_count = len(df[df["adjusted_setup_status"] == "watch"])
    weak_count = len(df[df["adjusted_setup_status"] == "weak"])
    total_count = len(df)

    print("\nSignal Agent finished.")
    print(f"Saved all setups to: {all_setups_path}")
    print(f"Saved actionable setups to: {top_setups_path}")

    print("\nRun summary:")
    print(f"Total candidates checked: {total_count}")
    print(f"Actionable: {actionable_count}")
    print(f"Watch: {watch_count}")
    print(f"Weak: {weak_count}")

    print("\nPreview:")
    print(df)


if __name__ == "__main__":
    main()