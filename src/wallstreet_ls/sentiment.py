from __future__ import annotations

import numpy as np
import pandas as pd


EVENT_COLUMNS = ("timestamp", "symbol", "source", "sentiment", "credibility", "novelty")


def load_sentiment_events(path: str) -> pd.DataFrame:
    events = pd.read_csv(path)
    missing = set(EVENT_COLUMNS) - set(events.columns)
    if missing:
        raise ValueError(f"Missing sentiment columns: {sorted(missing)}")
    events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True)
    for column in ("sentiment", "credibility", "novelty"):
        events[column] = pd.to_numeric(events[column], errors="raise").clip(-1 if column == "sentiment" else 0, 1)
    events["source"] = events["source"].str.lower()
    if not events["source"].isin(("news", "social")).all():
        raise ValueError("source must be news or social")
    return events.sort_values("timestamp")


def sentiment_scores_as_of(
    events: pd.DataFrame,
    as_of: pd.Timestamp,
    symbols: list[str],
    news_half_life_days: float = 3.0,
    social_half_life_days: float = 0.5,
) -> pd.DataFrame:
    """Aggregate only information published by as_of, with source-specific decay."""
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize("UTC")
    else:
        as_of = as_of.tz_convert("UTC")
    data = events.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True)
    data = data[(data["timestamp"] <= as_of) & data["symbol"].isin(symbols)]
    if data.empty:
        return pd.DataFrame(0.0, index=symbols, columns=["news", "social", "combined"])
    age_days = (as_of - data["timestamp"]).dt.total_seconds() / 86_400
    half_life = data["source"].map({"news": news_half_life_days, "social": social_half_life_days})
    decay = np.exp(-np.log(2) * age_days / half_life)
    data["weighted"] = data["sentiment"] * data["credibility"] * data["novelty"] * decay
    scores = data.pivot_table(index="symbol", columns="source", values="weighted", aggfunc="sum")
    scores = scores.reindex(index=symbols, columns=["news", "social"]).fillna(0).clip(-1, 1)
    scores["combined"] = 0.75 * scores["news"] + 0.25 * scores["social"]
    return scores
