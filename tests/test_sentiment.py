import pandas as pd
import pytest

from wallstreet_ls.sentiment import sentiment_scores_as_of


def test_sentiment_excludes_future_events_and_decays_social_faster():
    events = pd.DataFrame([
        {"timestamp": "2025-01-01T12:00:00Z", "symbol": "AAPL", "source": "news", "sentiment": 1, "credibility": 1, "novelty": 1},
        {"timestamp": "2025-01-01T12:00:00Z", "symbol": "AAPL", "source": "social", "sentiment": 1, "credibility": 1, "novelty": 1},
        {"timestamp": "2025-01-03T12:00:00Z", "symbol": "AAPL", "source": "news", "sentiment": -1, "credibility": 1, "novelty": 1},
    ])
    scores = sentiment_scores_as_of(events, pd.Timestamp("2025-01-02T12:00:00Z"), ["AAPL"])
    assert scores.loc["AAPL", "news"] == pytest.approx(2 ** (-1 / 3))
    assert scores.loc["AAPL", "social"] == pytest.approx(0.25)
    assert scores.loc["AAPL", "combined"] > 0
