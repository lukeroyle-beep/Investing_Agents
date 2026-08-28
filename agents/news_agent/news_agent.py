import os
from datetime import datetime, UTC

import pandas as pd

from shared.market_data import (
    MarketDataProvider,
    NewsDataResult,
    append_market_data_health_artifact,
    fetch_news,
    market_data_is_actionable,
    summarize_provider_error,
)
from shared.paths import FINAL_SHORTLIST_PATH, NEWS_FLAGS_PATH, NEWS_REVIEW_PATH


FINAL_SHORTLIST_FILE = FINAL_SHORTLIST_PATH
NEWS_REVIEW_FILE = NEWS_REVIEW_PATH
NEWS_FLAGS_FILE = NEWS_FLAGS_PATH


def load_final_shortlist():
    if not os.path.exists(FINAL_SHORTLIST_FILE):
        return pd.DataFrame()

    df = pd.read_csv(FINAL_SHORTLIST_FILE)

    if df.empty:
        return pd.DataFrame()

    return df


def classify_headline(headline):
    text = str(headline).lower()

    if any(word in text for word in ["earnings", "revenue", "profit", "guidance", "results"]):
        return "earnings_or_results"
    if any(word in text for word in ["upgrade", "downgrade", "rating", "price target", "analyst"]):
        return "analyst_or_rating"
    if any(word in text for word in ["acquisition", "merger", "buyout", "takeover"]):
        return "merger_or_acquisition"
    if any(word in text for word in ["regulation", "lawsuit", "legal", "sec", "fine", "probe", "investigation"]):
        return "regulation_or_legal"
    if any(word in text for word in ["launch", "product", "contract", "deal", "partnership", "expansion"]):
        return "product_or_business_update"
    if any(word in text for word in ["market", "economy", "fed", "rates", "inflation", "tariff"]):
        return "macro_or_market"

    return "other"


def fetch_news_for_ticker(
    ticker,
    name,
    market_data_provider: MarketDataProvider | None = None,
    health_results: list[NewsDataResult] | None = None,
):
    try:
        news_data = fetch_news(ticker, limit=10, provider=market_data_provider)
        if health_results is not None:
            health_results.append(news_data)
        news_items = news_data.items

        if not market_data_is_actionable(news_data):
            print(
                f"Skipping news for {ticker} ({name}) - data is not actionable: "
                f"{news_data.metadata.reason or news_data.metadata.error}"
            )
            return []

        if not news_items:
            return []

        results = []

        for item in news_items[:10]:
            title = item.get("title", "")
            publisher = item.get("publisher", "")
            link = item.get("link", "")
            provider_time = item.get("providerPublishTime", None)

            if provider_time:
                published_at = datetime.fromtimestamp(provider_time, UTC).isoformat()
            else:
                published_at = ""

            news_category = classify_headline(title)

            results.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "headline": title,
                    "publisher": publisher,
                    "link": link,
                    "published_at": published_at,
                    "news_category": news_category,
                    "checked_at": news_data.metadata.fetched_at,
                }
            )

        return results

    except Exception as e:
        raise RuntimeError(
            f"Unexpected news-provider failure for {ticker}: {summarize_provider_error(e)}"
        ) from e


def build_news_flags(news_df):
    if news_df.empty:
        return pd.DataFrame()

    grouped = (
        news_df.groupby(["ticker", "name"])
        .agg(
            headline_count=("headline", "count"),
            categories_found=("news_category", lambda x: ",".join(sorted(set(x))))
        )
        .reset_index()
    )

    grouped["has_news"] = grouped["headline_count"] > 0
    grouped["checked_at"] = datetime.now(UTC).isoformat()

    return grouped


def main():
    shortlist_df = load_final_shortlist()

    if shortlist_df.empty:
        print("No final shortlist found. Run the pipeline first.")
        return

    all_news_rows = []
    health_results: list[NewsDataResult] = []

    for _, row in shortlist_df.iterrows():
        ticker = row["ticker"]
        name = row["name"]

        print(f"Checking news for {ticker} ({name})...")
        news_rows = fetch_news_for_ticker(ticker, name, health_results=health_results)
        all_news_rows.extend(news_rows)

    if all_news_rows:
        news_df = pd.DataFrame(all_news_rows)
    else:
        news_df = pd.DataFrame(
            columns=[
                "ticker",
                "name",
                "headline",
                "publisher",
                "link",
                "published_at",
                "news_category",
                "checked_at",
            ]
        )

    flags_df = build_news_flags(news_df)

    NEWS_REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    append_market_data_health_artifact(health_results)

    news_df.to_csv(NEWS_REVIEW_FILE, index=False)
    flags_df.to_csv(NEWS_FLAGS_FILE, index=False)

    total_headlines = len(news_df)
    total_flagged_tickers = len(flags_df)

    print("\nNews Agent finished.")
    print(f"Saved news review to: {NEWS_REVIEW_FILE}")
    print(f"Saved news flags to: {NEWS_FLAGS_FILE}")

    print("\nRun summary:")
    print(f"Total shortlisted tickers checked: {len(shortlist_df)}")
    print(f"Total headlines captured: {total_headlines}")
    print(f"Tickers with news flags: {total_flagged_tickers}")

    print("\nNews flags preview:")
    print(flags_df)


if __name__ == "__main__":
    main()
