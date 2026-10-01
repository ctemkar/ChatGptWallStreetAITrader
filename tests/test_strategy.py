import numpy as np
import pandas as pd

from wallstreet_ls.cli import stock_count_for_strategy
from wallstreet_ls.config import StrategyConfig
from wallstreet_ls.strategy import calculate_diversified_momentum_targets, calculate_targets, calculate_momentum_targets


def test_targets_are_long_short_capped_and_near_neutral():
    rng = np.random.default_rng(7)
    dates = pd.date_range("2024-01-01", periods=180, freq="B")
    symbols = [f"S{i}" for i in range(12)]
    drifts = np.linspace(-0.001, 0.001, len(symbols))
    returns = rng.normal(drifts, 0.012, (len(dates), len(symbols)))
    closes = pd.DataFrame(50 * np.exp(np.cumsum(returns, axis=0)), index=dates, columns=symbols)
    volumes = pd.DataFrame(2_000_000.0, index=dates, columns=symbols)
    config = StrategyConfig(
        symbols=tuple(symbols), long_count=3, short_count=3,
        max_position_weight=0.20, min_average_dollar_volume=1,
    )

    targets = calculate_targets(closes, volumes, config)

    assert (targets.target_weight > 0).sum() == 3
    assert (targets.target_weight < 0).sum() == 3
    assert targets.target_weight.abs().max() <= 0.20
    assert targets.target_weight.abs().sum() <= config.max_gross_exposure
    assert abs(targets.target_weight.sum()) < 0.08


def test_rejects_insufficient_history():
    frame = pd.DataFrame({"A": [10.0] * 20})
    config = StrategyConfig(momentum_lookback=30, min_average_dollar_volume=1)
    try:
        calculate_targets(frame, frame * 1000, config)
    except ValueError as exc:
        assert "observations" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_momentum_targets_are_long_only():
    dates = pd.date_range("2020-01-01", periods=140, freq="B")
    closes = pd.DataFrame({"A": range(100, 240), "B": range(100, 380, 2), "C": range(100, 520, 3)}, index=dates)
    volumes = pd.DataFrame(1_000_000.0, index=dates, columns=closes.columns)
    targets = calculate_momentum_targets(closes, volumes, StrategyConfig(min_average_dollar_volume=1), stock_count=2)
    assert len(targets) == 2
    assert (targets.target_weight > 0).all()


def test_diversified_momentum_targets_are_equal_weighted_and_long_only():
    dates = pd.date_range("2020-01-01", periods=140, freq="B")
    closes = pd.DataFrame({
        "A": range(100, 240), "B": range(100, 380, 2), "C": range(100, 520, 3),
        "D": range(100, 660, 4),
    }, index=dates)
    volumes = pd.DataFrame(1_000_000.0, index=dates, columns=closes.columns)
    targets = calculate_diversified_momentum_targets(closes, volumes, StrategyConfig(min_average_dollar_volume=1), stock_count=3)
    assert len(targets) == 3
    assert (targets.target_weight == 1 / 3).all()
    assert (targets.target_weight > 0).all()


def test_replacement_strategy_uses_validated_default_basket_size():
    assert stock_count_for_strategy("diversified-momentum", None) == 20
    assert stock_count_for_strategy("momentum", None) == 10
    assert stock_count_for_strategy("diversified-momentum", 12) == 12
