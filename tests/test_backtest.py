import numpy as np
import pandas as pd
import pytest

from wallstreet_ls.backtest import run_backtest, run_enhanced_backtest
from wallstreet_ls.config import StrategyConfig


def test_backtest_starts_at_capital_and_charges_costs():
    rng = np.random.default_rng(2)
    dates = pd.date_range("2018-01-01", periods=700, freq="B", tz="UTC")
    symbols = [f"S{i}" for i in range(10)]
    returns = rng.normal(np.linspace(-0.0005, 0.0007, 10), 0.01, (len(dates), 10))
    closes = pd.DataFrame(40 * np.exp(np.cumsum(returns, axis=0)), index=dates, columns=symbols)
    closes["SPY"] = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.008, len(dates))))
    volumes = pd.DataFrame(1_000_000.0, index=dates, columns=closes.columns)
    config = StrategyConfig(symbols=tuple(symbols), long_count=2, short_count=2, momentum_lookback=60, momentum_skip=10, min_average_dollar_volume=1)
    curve, metrics = run_backtest(closes, volumes, config, start="2019-01-01")
    assert len(curve) > 200
    assert metrics["assumptions"]["initial_capital"] == 2200
    assert metrics["average_monthly_turnover"] > 0
    assert set(metrics["comparison"]) == {
        "correlation", "beta", "annualized_alpha_zero_rf", "tracking_error",
        "information_ratio", "sortino_zero_rf",
    }
    assert np.isfinite(list(metrics["comparison"].values())).all()
    assert curve.strategy_equity.notna().all()


def test_short_borrow_cost_reduces_long_short_backtest_returns():
    rng = np.random.default_rng(9)
    dates = pd.date_range("2018-01-01", periods=700, freq="B", tz="UTC")
    symbols = [f"S{i}" for i in range(10)]
    returns = rng.normal(np.linspace(-0.0004, 0.0006, 10), 0.01, (len(dates), 10))
    closes = pd.DataFrame(40 * np.exp(np.cumsum(returns, axis=0)), index=dates, columns=symbols)
    closes["SPY"] = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.008, len(dates))))
    volumes = pd.DataFrame(1_000_000.0, index=dates, columns=closes.columns)
    config = StrategyConfig(symbols=tuple(symbols), long_count=2, short_count=2,
                            momentum_lookback=60, momentum_skip=10, min_average_dollar_volume=1)
    base, _ = run_backtest(closes, volumes, config, start="2019-01-01")
    stressed, metrics = run_backtest(closes, volumes, config, start="2019-01-01", short_borrow_bps_annual=500)
    assert stressed.strategy_equity.iloc[-1] < base.strategy_equity.iloc[-1]
    assert metrics["assumptions"]["short_borrow_bps_annual"] == 500


def test_enhanced_backtest_stays_long_and_tracks_benchmark():
    rng = np.random.default_rng(7)
    dates = pd.date_range("2018-01-01", periods=700, freq="B", tz="UTC")
    symbols = [f"S{i}" for i in range(10)]
    returns = rng.normal(np.linspace(0.0001, 0.0008, 10), 0.01, (len(dates), 10))
    closes = pd.DataFrame(40 * np.exp(np.cumsum(returns, axis=0)), index=dates, columns=symbols)
    closes["SPY"] = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.008, len(dates))))
    volumes = pd.DataFrame(1_000_000.0, index=dates, columns=closes.columns)
    config = StrategyConfig(symbols=tuple(symbols), momentum_lookback=60, momentum_skip=10, min_average_dollar_volume=1)
    curve, metrics = run_enhanced_backtest(closes, volumes, config, start="2019-01-01", sleeve_count=3)
    assert curve.strategy_equity.notna().all()
    assert metrics["assumptions"]["core_weight"] == 0.9
    assert metrics["assumptions"]["factor_sleeve_weight"] == pytest.approx(0.1)
    assert metrics["comparison"]["beta"] > 0.5


def test_backtest_excludes_a_final_rebalance_without_a_forward_return_period(monkeypatch):
    dates = pd.date_range("2020-01-01", "2020-03-31", freq="B", tz="UTC")
    closes = pd.DataFrame({
        "A": np.linspace(100, 120, len(dates)),
        "B": np.linspace(120, 100, len(dates)),
        "SPY": np.linspace(100, 110, len(dates)),
    }, index=dates)
    volumes = pd.DataFrame(1_000_000.0, index=dates, columns=closes.columns)
    config = StrategyConfig(
        symbols=("A", "B"), long_count=1, short_count=1,
        momentum_lookback=10, momentum_skip=2, volatility_lookback=5,
        min_average_dollar_volume=1,
    )
    calls = []

    def targets(history_close, history_volume, strategy_config):
        calls.append(history_close.index[-1])
        return pd.DataFrame({"target_weight": [0.3, -0.3]}, index=["A", "B"])

    monkeypatch.setattr("wallstreet_ls.backtest.calculate_targets", targets)
    _, metrics = run_backtest(closes, volumes, config, start="2020-01-01")
    # Jan and Feb signals have following months; the Mar 31 signal cannot be
    # executed in this sample and must not inflate average turnover.
    assert len(calls) == 2
    assert calls[-1].month == 2
    assert metrics["average_monthly_turnover"] > 0
