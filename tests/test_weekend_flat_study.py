"""Execution tests for the weekend study. No network and no broker."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))

from weekend_flat_study import (  # noqa: E402
    fee_schedule,
    last_session_of_each_week,
    make_fixed_weight_fn,
    point_in_time_closes,
    regulatory_sell_fee,
    simulate,
)
from wallstreet_ls.config import StrategyConfig
from wallstreet_ls.strategy import calculate_targets


def _frames(dates, symbols, opens, closes):
    index = pd.DatetimeIndex(dates)
    open_df = pd.DataFrame(opens, index=index, columns=symbols, dtype=float)
    close_df = pd.DataFrame(closes, index=index, columns=symbols, dtype=float)
    volume = pd.DataFrame(1_000_000.0, index=index, columns=symbols)
    return open_df, close_df, volume


def _run(dates, symbols, opens, closes, *, flatten, rebalance, weights, cost_bps=0.0, fee_mode="none", capital=100.0):
    open_df, close_df, volume = _frames(dates, symbols, opens, closes)
    config = StrategyConfig(
        starting_capital=capital,
        max_position_weight=1.0,
        max_gross_exposure=1.0,
        gross_exposure=1.0,
        min_average_dollar_volume=1.0,
    )
    return simulate(
        open_df, close_df, open_df, close_df, volume,
        {pd.Timestamp(d).normalize() for d in rebalance},
        make_fixed_weight_fn(weights),
        starting_capital=capital,
        config=config,
        flatten_weekends=flatten,
        cost_bps=cost_bps,
        fee_mode=fee_mode,
        sizing_mode="full",
    )


def test_holiday_week_uses_thursday_and_known_friday_stays_open():
    good_friday_week = pd.to_datetime(["2024-03-25", "2024-03-26", "2024-03-27", "2024-03-28", "2024-04-01"])
    last = last_session_of_each_week(good_friday_week)
    assert pd.Timestamp("2024-03-28") in last
    assert pd.Timestamp("2024-03-27") not in last

    normal = pd.to_datetime(["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07", "2024-06-10"])
    last = last_session_of_each_week(normal)
    assert pd.Timestamp("2024-06-07") in last
    assert pd.Timestamp("2024-06-06") not in last


def test_negative_weekend_gap_is_avoided_and_friday_session_is_not():
    dates = pd.to_datetime(["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07", "2024-06-10"])
    # Monday entry at 100, flat through Friday close, Monday open gaps to 80.
    opens = [[100], [100], [100], [100], [100], [80]]
    closes = [[100], [100], [100], [100], [100], [80]]
    hold = _run(dates, ["AAA"], opens, closes, flatten=False, rebalance=[dates[0]], weights={"AAA": 1.0})
    flat = _run(dates, ["AAA"], opens, closes, flatten=True, rebalance=[dates[0]], weights={"AAA": 1.0})
    assert hold["ending_equity"] == 80
    assert flat["ending_equity"] == 100

    # The same decline inside Friday is earned by both policies.
    opens = [[100], [100], [100], [100], [100], [70]]
    closes = [[100], [100], [100], [100], [70], [70]]
    hold = _run(dates, ["AAA"], opens, closes, flatten=False, rebalance=[dates[0]], weights={"AAA": 1.0})
    flat = _run(dates, ["AAA"], opens, closes, flatten=True, rebalance=[dates[0]], weights={"AAA": 1.0})
    assert hold["ending_equity"] == 70
    assert flat["ending_equity"] == 70


def test_friday_holiday_flattens_on_thursday():
    dates = pd.to_datetime(["2024-03-25", "2024-03-26", "2024-03-27", "2024-03-28", "2024-04-01"])
    opens = [[100], [100], [100], [100], [80]]
    closes = [[100], [100], [100], [100], [80]]
    hold = _run(dates, ["AAA"], opens, closes, flatten=False, rebalance=[dates[0]], weights={"AAA": 1.0})
    flat = _run(dates, ["AAA"], opens, closes, flatten=True, rebalance=[dates[0]], weights={"AAA": 1.0})
    assert hold["ending_equity"] == 80
    assert flat["ending_equity"] == 100


def test_sample_ending_on_friday_does_not_flatten_an_unfinished_weekend():
    dates = pd.to_datetime(["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07"])
    opens = [[100]] * 5
    closes = [[100]] * 5
    flat = _run(dates, ["AAA"], opens, closes, flatten=True, rebalance=[dates[0]], weights={"AAA": 1.0})
    assert flat["ending_equity"] == 100
    assert flat["totals"]["traded_notional"] == 100
    assert len(flat["weekends"]) == 0


def test_round_trip_cost_is_about_two_sides_and_rebalance_is_not_doubled():
    dates = pd.to_datetime(["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07", "2024-06-10"])
    opens = [[100]] * 6
    closes = [[100]] * 6
    hold = _run(
        dates, ["AAA"], opens, closes, flatten=False, rebalance=[dates[0]],
        weights={"AAA": 1.0}, cost_bps=100,
    )
    flat = _run(
        dates, ["AAA"], opens, closes, flatten=True, rebalance=[dates[0]],
        weights={"AAA": 1.0}, cost_bps=100,
    )
    assert hold["ending_equity"] == 99
    assert 97.0 < flat["ending_equity"] < 97.1

    # Month-end Friday: buy the new book once, at Friday's close if holding
    # and at Monday's open if flattening. Do not buy Friday and sell Friday.
    gap_open = [[100], [100], [100], [100], [100], [110]]
    gap_close = [[100], [100], [100], [100], [100], [110]]
    hold = _run(dates, ["AAA"], gap_open, gap_close, flatten=False, rebalance=[dates[4]], weights={"AAA": 1.0})
    flat = _run(dates, ["AAA"], gap_open, gap_close, flatten=True, rebalance=[dates[4]], weights={"AAA": 1.0})
    assert hold["ending_equity"] == 110
    assert abs(flat["ending_equity"] - 100) < 1e-9
    assert abs(hold["totals"]["traded_notional"] - 100) < 1e-9
    assert abs(flat["totals"]["traded_notional"] - 100) < 1e-9


def test_short_book_weekend_gap_has_the_opposite_sign():
    dates = pd.to_datetime(["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07", "2024-06-10"])
    opens = [[100], [100], [100], [100], [100], [110]]
    closes = [[100], [100], [100], [100], [100], [110]]
    hold = _run(dates, ["AAA"], opens, closes, flatten=False, rebalance=[dates[0]], weights={"AAA": -1.0})
    flat = _run(dates, ["AAA"], opens, closes, flatten=True, rebalance=[dates[0]], weights={"AAA": -1.0})
    assert hold["ending_equity"] == 90
    assert flat["ending_equity"] == 100


def test_disclosed_sell_fee_hits_the_flattening_sale_only():
    dates = pd.to_datetime(["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07", "2024-06-10"])
    opens = [[100]] * 6
    closes = [[100]] * 6
    hold = _run(
        dates, ["AAA"], opens, closes, flatten=False, rebalance=[dates[0]],
        weights={"AAA": 1.0}, fee_mode="disclosed",
    )
    flat = _run(
        dates, ["AAA"], opens, closes, flatten=True, rebalance=[dates[0]],
        weights={"AAA": 1.0}, fee_mode="disclosed",
    )
    schedule = fee_schedule(date(2024, 6, 7), "disclosed")
    assert schedule.sec_per_dollar == 27.80 / 1_000_000
    assert schedule.sec_is_estimate is False
    assert hold["totals"]["reg"] == 0
    expected = regulatory_sell_fee(1, 100, schedule)
    assert flat["totals"]["reg"] == expected
    assert expected > 0


def test_weight_function_cannot_see_prices_after_the_decision():
    dates = pd.to_datetime(["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-28", "2024-07-01"])
    seen = []

    def weight_fn(day, adj_hist, raw_hist, vol_hist, equity):
        assert adj_hist.index.max() <= pd.Timestamp(day)
        assert raw_hist.index.max() <= pd.Timestamp(day)
        seen.append(pd.Timestamp(day))
        return pd.Series({"AAA": 1.0})

    index = pd.DatetimeIndex(dates)
    price = pd.DataFrame({"AAA": [10.0, 10.0, 10.0, 10.0, 99.0]}, index=index)
    volume = pd.DataFrame({"AAA": 1.0}, index=index)
    simulate(
        price, price, price, price, volume,
        {pd.Timestamp("2024-06-28")},
        weight_fn,
        starting_capital=100,
        config=StrategyConfig(starting_capital=100, max_position_weight=1, max_gross_exposure=1, gross_exposure=1),
        flatten_weekends=False,
        sizing_mode="full",
    )
    assert seen == [pd.Timestamp("2024-06-28")]


def test_point_in_time_rescale_keeps_the_short_cap_on_the_tradeable_price():
    rng_dates = pd.bdate_range("2020-01-01", periods=180)
    symbols = [f"S{i}" for i in range(12)]
    adj = pd.DataFrame(index=rng_dates, columns=symbols, dtype=float)
    raw = pd.DataFrame(index=rng_dates, columns=symbols, dtype=float)
    for i, sym in enumerate(symbols):
        path = np.linspace(50, 70 + i, len(rng_dates))
        adj[sym] = path
        raw[sym] = path
    adj["S0"] = np.linspace(100, 40, len(rng_dates))
    raw["S0"] = adj["S0"] * 10  # future split/dividend factor of 0.1 baked into adj
    pit = point_in_time_closes(adj, raw)
    assert pit.iloc[-1]["S0"] == raw.iloc[-1]["S0"]
    volumes = pd.DataFrame(1_000_000.0, index=rng_dates, columns=symbols)
    config = StrategyConfig(min_average_dollar_volume=1.0)
    capped = calculate_targets(pit, volumes, config)
    naive = calculate_targets(adj, volumes, config)
    assert "S0" in naive.index and naive.loc["S0", "target_weight"] < 0
    assert ("S0" not in capped.index) or capped.loc["S0", "target_weight"] >= 0


def test_future_level_shift_does_not_change_an_earlier_target():
    dates = pd.bdate_range("2020-01-01", periods=180)
    symbols = [f"S{i}" for i in range(12)]
    closes = pd.DataFrame(
        {sym: np.linspace(40, 60 + i * 3, len(dates)) for i, sym in enumerate(symbols)},
        index=dates,
    )
    volumes = pd.DataFrame(5_000_000.0, index=dates, columns=symbols)
    config = StrategyConfig(min_average_dollar_volume=1.0)
    asof = dates[150]
    base = calculate_targets(closes.loc[:asof], volumes.loc[:asof], config)
    shocked = closes.copy()
    shocked.iloc[151:] *= 10
    later = calculate_targets(shocked.loc[:asof], volumes.loc[:asof], config)
    pd.testing.assert_series_equal(base["target_weight"], later["target_weight"])
