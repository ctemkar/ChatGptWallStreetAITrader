import pandas as pd

from wallstreet_ls.scalping_backtest import run_quick_flip


def test_quick_flip_returns_empty_metrics_when_no_sweep():
    index = pd.date_range("2025-01-02 14:30", periods=19, freq="5min", tz="UTC")
    bars = pd.DataFrame({"open": 100, "high": 100.1, "low": 99.9, "close": 100, "volume": 1000}, index=index)
    trades, metrics = run_quick_flip(bars)
    assert trades.empty
    assert metrics["ending_equity"] == 2200
