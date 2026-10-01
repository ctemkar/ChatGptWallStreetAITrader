from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os

import pandas as pd
from dotenv import load_dotenv

from .config import StrategyConfig


POSITIVE = {"beat", "beats", "growth", "raises", "raised", "upgrade", "record", "profit", "surge", "wins", "approval"}
NEGATIVE = {"miss", "misses", "cuts", "cut", "downgrade", "loss", "lawsuit", "probe", "recall", "fraud", "decline"}


def headline_sentiment(text: str) -> float:
    words = {word.strip(".,:;!?()[]'\"").lower() for word in text.split()}
    score = len(words & POSITIVE) - len(words & NEGATIVE)
    return float(max(-1, min(1, score / 2)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Download point-in-time Alpaca news signals")
    parser.add_argument("--start", default="2020-07-01")
    parser.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--output", default="news_sentiment_events.csv")
    args = parser.parse_args()
    load_dotenv(".env.local")
    load_dotenv()

    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    client = NewsClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    months = pd.date_range(args.start, args.end, freq="MS", tz="UTC")
    rows: list[dict] = []
    for start in months:
        end = min(start + pd.offsets.MonthBegin(1), pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1))
        result = client.get_news(NewsRequest(
            start=start.to_pydatetime(),
            end=end.to_pydatetime(),
            symbols=",".join(StrategyConfig().symbols),
            sort="asc",
            limit=50,
            include_content=False,
        ))
        for article in result.data["news"]:
            headline = article.headline
            for symbol in article.symbols:
                if symbol in StrategyConfig().symbols:
                    rows.append({
                        "timestamp": article.created_at.isoformat(),
                        "symbol": symbol,
                        "source": "news",
                        "sentiment": headline_sentiment(headline),
                        "credibility": 0.9,
                        "novelty": 1.0,
                    })
    pd.DataFrame(rows).drop_duplicates().to_csv(args.output, index=False)
    print(f"Saved {len(rows)} timestamped news-symbol events to {args.output}")


if __name__ == "__main__":
    main()
