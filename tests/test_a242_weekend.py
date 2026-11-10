"""A242 weekend mechanics. No network and no broker."""
from datetime import date
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a242_weekend_study import a242_config, simulate_a242, trading_days_between
from wallstreet_ls.strategy import calculate_targets


def test_reentry_cooldown_counts_weekdays_only():
    # Friday close, next Wednesday is 3 weekdays (Mon, Tue, Wed).
    assert trading_days_between(date(2026, 10, 16), date(2026, 10, 21)) == 3
    assert trading_days_between(date(2026, 10, 16), date(2026, 10, 20)) == 2


def test_a242_engine_is_four_longs_with_half_the_configured_gross():
    dates = pd.bdate_range("2020-01-01", periods=280)
    symbols = [f"S{i}" for i in range(12)]
    closes = pd.DataFrame(
        {sym: np.linspace(40, 70 + i * 4, len(dates)) for i, sym in enumerate(symbols)},
        index=dates,
    )
    volumes = pd.DataFrame(2_000_000.0, index=dates, columns=symbols)
    config = a242_config()
    targets = calculate_targets(closes, volumes, config, exclude_from_shorts=set(symbols))
    assert (targets.target_weight > 0).sum() == 4
    assert (targets.target_weight < 0).sum() == 0
    assert targets.target_weight.sum() <= 0.95 + 1e-9
    assert targets.target_weight.max() <= 0.40 + 1e-9


def test_flatten_gives_up_a_positive_gap_and_pays_both_sides():
    dates = pd.to_datetime(["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"])
    # Enter Monday, hold a flat week, Friday close 100, Monday open 110.
    opens = [100, 100, 100, 100, 100, 110]
    closes = [100, 100, 100, 100, 100, 110]
    price = pd.DataFrame({"AAA": closes}, index=dates)
    open_px = pd.DataFrame({"AAA": opens}, index=dates)
    signals = pd.DataFrame({"AAA": [1.0] * len(dates)}, index=dates)
    # The engine budget is not used here; the injected signal is fully invested.
    hold = simulate_a242(open_px, price, open_px, price, signals, flatten=False, cost_bps=0, fee_mode="none", capital=100)
    flat = simulate_a242(open_px, price, open_px, price, signals, flatten=True, cost_bps=0, fee_mode="none", capital=100)
    assert abs(hold["ending"] - 110) < 1e-6
    assert abs(flat["ending"] - 100) < 1e-6
    costly = simulate_a242(open_px, price, open_px, price, signals, flatten=True, cost_bps=10, fee_mode="none", capital=100)
    assert costly["ending"] < flat["ending"]
    assert costly["slippage"] > 0
