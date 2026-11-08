"""Research-only weekend hold vs flatten study.

This is not the A242 strategy. A242 is not in this repository. The long/short
path below calls ``wallstreet_ls.strategy.calculate_targets`` — the book the
live desk scripts in this repo submit. SPY and the four-name basket are
controls, not A242.

No broker client, no credentials, no orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import math
import subprocess
import time
import urllib.request

import numpy as np
import pandas as pd

from wallstreet_ls.config import StrategyConfig
from wallstreet_ls.strategy import calculate_targets


ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "research" / "weekend_flat_output"

# SEC fee-rate advisory, Feb 27, 2026: $20.60 per $1,000,000 of sales from
# April 4, 2026. Earlier published steps used here are May 14, 2025 ($0) and
# May 22, 2024 ($27.80). Before May 22, 2024 this study applies $8.00 per
# $1,000,000 as a disclosed estimate, not a reconstructed rate history.
# FINRA TAF: $0.000166 / $8.30 cap for 2024-2025; $0.000195 / $9.79 for
# calendar 2026 through Sep 30; $0 from Oct 1, 2026 through Dec 31, 2026
# (SR-FINRA-2026-021). Pre-2024 TAF uses the 2024 rate as an estimate.
SEC_PER_MILLION_ESTIMATE_PRE_2024_05_22 = 8.00
TAF_PRE_2024_ESTIMATE = 0.000166
TAF_PRE_2024_CAP = 8.30

# Full-day NYSE closures used only to validate the session calendar. Dates
# after the downloaded sample are ignored. This is not a parameter search.
_HOLIDAYS = (
    "2018-03-30", "2019-04-19", "2020-04-10", "2021-04-02", "2022-04-15",
    "2023-04-07", "2024-03-29", "2025-04-18", "2026-04-03",
    "2018-11-22", "2019-11-28", "2020-11-26", "2021-11-25", "2022-11-24",
    "2023-11-23", "2024-11-28", "2025-11-27", "2026-11-26",
    "2018-07-04", "2019-07-04", "2020-07-03", "2021-07-05", "2022-07-04",
    "2023-07-04", "2024-07-04", "2025-07-04", "2026-07-03",
    "2019-12-25", "2020-12-25", "2022-12-26", "2023-12-25", "2024-12-25",
    "2025-12-25", "2026-12-25",
)
_MUST_BE_OPEN = (
    "2024-07-05", "2025-11-28", "2026-07-02", "2026-07-06", "2026-11-06",
)

SNAPSHOT_SYMBOLS = ("AMD", "CVX", "MRK", "XOM")
LIVE_START = pd.Timestamp("2026-10-08")
HOLDOUT_START = pd.Timestamp("2023-01-01")
HOLDOUT_END = pd.Timestamp("2026-10-07")


@dataclass(frozen=True)
class FeeSchedule:
    sec_per_dollar: float
    taf_per_share: float
    taf_cap: float
    sec_is_estimate: bool
    taf_is_estimate: bool


def fee_schedule(day: date, mode: str) -> FeeSchedule:
    if mode == "none":
        return FeeSchedule(0.0, 0.0, 0.0, False, False)
    if mode != "disclosed":
        raise ValueError(mode)
    if day >= date(2026, 4, 4):
        sec, sec_est = 20.60 / 1_000_000, False
    elif day >= date(2025, 5, 14):
        sec, sec_est = 0.0, False
    elif day >= date(2024, 5, 22):
        sec, sec_est = 27.80 / 1_000_000, False
    else:
        sec, sec_est = SEC_PER_MILLION_ESTIMATE_PRE_2024_05_22 / 1_000_000, True
    if date(2026, 10, 1) <= day <= date(2026, 12, 31):
        taf, cap, taf_est = 0.0, 0.0, False
    elif day >= date(2026, 1, 1):
        taf, cap, taf_est = 0.000195, 9.79, False
    elif day >= date(2024, 1, 1):
        taf, cap, taf_est = 0.000166, 8.30, False
    else:
        taf, cap, taf_est = TAF_PRE_2024_ESTIMATE, TAF_PRE_2024_CAP, True
    return FeeSchedule(sec, taf, cap, sec_est, taf_est)


def regulatory_sell_fee(real_shares_sold: float, sale_notional: float, fee: FeeSchedule) -> float:
    if real_shares_sold <= 0 or sale_notional <= 0:
        return 0.0
    sec = sale_notional * fee.sec_per_dollar
    if fee.taf_per_share <= 0 or fee.taf_cap <= 0:
        return sec
    taf = min(fee.taf_cap, math.ceil(real_shares_sold - 1e-12) * fee.taf_per_share)
    return sec + taf


def last_session_of_each_week(index: pd.DatetimeIndex) -> set[pd.Timestamp]:
    """Last print of each NYSE week. A Friday holiday makes Thursday the last."""
    index = pd.DatetimeIndex(pd.to_datetime(index)).tz_localize(None).normalize()
    if index.empty:
        return set()
    week = index.to_period("W-FRI")
    last = pd.Series(index, index=week).groupby(level=0).max()
    return {pd.Timestamp(v).normalize() for v in last.tolist()}


def month_end_sessions(index: pd.DatetimeIndex) -> set[pd.Timestamp]:
    index = pd.DatetimeIndex(pd.to_datetime(index)).tz_localize(None).normalize()
    if index.empty:
        return set()
    month = index.to_period("M")
    last = pd.Series(index, index=month).groupby(level=0).max()
    return {pd.Timestamp(v).normalize() for v in last.tolist()}


def validate_session_calendar(index: pd.DatetimeIndex) -> dict:
    days = set(pd.DatetimeIndex(index).tz_localize(None).normalize())
    weekend = [str(d.date()) for d in days if pd.Timestamp(d).weekday() >= 5]
    if weekend:
        raise RuntimeError(f"session calendar contains weekend dates: {weekend[:5]}")
    start, end = min(days), max(days)
    missing = []
    for iso in _HOLIDAYS:
        stamp = pd.Timestamp(iso)
        if start <= stamp <= end and stamp in days:
            missing.append(iso)
    if missing:
        raise RuntimeError(f"expected full closures printed as sessions: {missing}")
    absent = []
    for iso in _MUST_BE_OPEN:
        stamp = pd.Timestamp(iso)
        if start <= stamp <= end and stamp not in days:
            absent.append(iso)
    if absent:
        raise RuntimeError(f"expected open sessions missing: {absent}")
    week_last = last_session_of_each_week(pd.DatetimeIndex(sorted(days)))
    checks = {
        "good_friday_2024_last_session": str(max(d for d in days if d <= pd.Timestamp("2024-03-29")).date())
        if any(d <= pd.Timestamp("2024-03-29") for d in days) else None,
        "good_friday_2025_last_session": str(max(d for d in days if d <= pd.Timestamp("2025-04-18")).date())
        if any(d <= pd.Timestamp("2025-04-18") for d in days) else None,
        "independence_2026_observed_last_before": str(max(d for d in days if d <= pd.Timestamp("2026-07-03")).date())
        if any(d <= pd.Timestamp("2026-07-03") for d in days) else None,
        "thanksgiving_2025_week_last": None,
    }
    # Thanksgiving 2025 is Thursday 11-27; Friday 11-28 is a regular session.
    if pd.Timestamp("2025-11-28") in days:
        checks["thanksgiving_2025_week_last"] = str(
            max(d for d in week_last if pd.Timestamp("2025-11-24") <= d <= pd.Timestamp("2025-11-28")).date()
        )
        if checks["thanksgiving_2025_week_last"] != "2025-11-28":
            raise RuntimeError("Thanksgiving week last session is not Friday 2025-11-28")
    if checks["good_friday_2024_last_session"] not in (None, "2024-03-28"):
        raise RuntimeError(checks)
    if checks["good_friday_2025_last_session"] not in (None, "2025-04-17"):
        raise RuntimeError(checks)
    if checks["independence_2026_observed_last_before"] not in (None, "2026-07-02"):
        raise RuntimeError(checks)
    checks["n_sessions"] = len(days)
    checks["weekend_bars"] = 0
    return checks


def point_in_time_closes(adj_hist: pd.DataFrame, raw_hist: pd.DataFrame) -> pd.DataFrame:
    """Rescale a backward-adjusted window so the last row is the tradeable close.

    A full-sample adjustment factor includes splits and dividends after the
    decision date. Those future factors are common to every row in a ratio, so
    momentum is unchanged, but they shrink the price level and would make the
    $220 short cap look backward. Multiplying by raw_last/adj_last puts the
    decision price on the tradeable close and leaves only corporate actions
    already inside the window in the history.
    """
    raw_last = raw_hist.iloc[-1].astype(float)
    adj_last = adj_hist.iloc[-1].astype(float).replace(0, np.nan)
    scale = raw_last / adj_last
    return adj_hist.astype(float).multiply(scale, axis=1)


def mark_to_market(cash: float, shares: pd.Series, price: pd.Series) -> float:
    value = cash
    for sym, qty in shares.items():
        if qty == 0:
            continue
        px = price[sym]
        if not np.isfinite(px):
            raise ValueError(f"cannot mark {sym} with {qty} shares")
        value += float(qty) * float(px)
    return float(value)


def weights_to_shares(
    weights: pd.Series,
    equity: float,
    adj_price: pd.Series,
    raw_price: pd.Series,
    config: StrategyConfig,
    sizing_mode: str,
) -> pd.Series:
    """Convert target weights to shares in adjusted-price units.

    ``live_cap`` matches ``build_trade_plan``: size off min(equity, starting
    capital), and round shorts toward zero shares with a one-share floor only
    when one share still fits under the position cap.
    """
    if sizing_mode == "live_cap":
        sizing = min(float(equity), float(config.starting_capital))
    elif sizing_mode == "full":
        sizing = float(equity)
    else:
        raise ValueError(sizing_mode)
    target = pd.Series(0.0, index=adj_price.index, dtype=float)
    if sizing <= 0:
        return target
    for sym, weight in weights.items():
        weight = float(weight)
        if weight == 0 or sym not in target.index:
            continue
        raw_px = float(raw_price[sym]) if sym in raw_price.index else np.nan
        adj_px = float(adj_price[sym]) if sym in adj_price.index else np.nan
        if not np.isfinite(raw_px) or raw_px <= 0 or not np.isfinite(adj_px) or adj_px <= 0:
            continue
        if weight < 0:
            one_share_weight = raw_px / sizing
            if one_share_weight > config.max_position_weight:
                continue
            real = float(int(sizing * weight / raw_px))  # truncates toward zero
            if real == 0:
                real = -1.0
            notional = real * raw_px
            target[sym] = notional / adj_px
        else:
            target[sym] = sizing * weight / adj_px
    return target


def trade_to(
    cash: float,
    shares: pd.Series,
    target: pd.Series,
    exec_price: pd.Series,
    raw_price: pd.Series,
    cost_bps: float,
    fee: FeeSchedule,
) -> tuple[float, pd.Series, dict]:
    """Trade from ``shares`` toward ``target`` at ``exec_price``.

    Buys fill at print * (1 + bps). Sells fill at print * (1 - bps). SEC/TAF
    apply to sells only, including short openings. TAF share count is the
    tradeable share count (notional / raw price), not the adjusted-unit share.
    """
    slip_rate = cost_bps / 10_000.0
    new_shares = shares.copy().astype(float)
    slippage = 0.0
    reg = 0.0
    reg_estimated = 0.0
    traded = 0.0
    stuck = 0
    for sym in shares.index:
        qty = float(shares[sym])
        dest = float(target[sym]) if sym in target.index else 0.0
        delta = dest - qty
        px = float(exec_price[sym]) if sym in exec_price.index else np.nan
        if not np.isfinite(px) or px <= 0:
            if abs(delta) * max(px, 0) > 1 or abs(delta) > 1e-8:
                stuck += 1
            continue
        if abs(delta) * px < 0.01:
            continue
        new_shares[sym] = qty + delta
        traded += abs(delta) * px
        if delta > 0:
            slip = delta * px * slip_rate
            cash -= delta * px + slip
            slippage += slip
        else:
            sold_adj = -delta
            slip = sold_adj * px * slip_rate
            sale_notional = sold_adj * px - slip
            cash += sale_notional
            slippage += slip
            raw_px = float(raw_price[sym]) if sym in raw_price.index else np.nan
            real_sold = sold_adj * px / raw_px if np.isfinite(raw_px) and raw_px > 0 else sold_adj
            fee_dollars = regulatory_sell_fee(real_sold, sale_notional, fee)
            cash -= fee_dollars
            reg += fee_dollars
            if fee.sec_is_estimate or fee.taf_is_estimate:
                reg_estimated += fee_dollars
    stats = {
        "slippage": slippage,
        "reg": reg,
        "reg_estimated": reg_estimated,
        "traded_notional": traded,
        "stuck": stuck,
    }
    return cash, new_shares, stats


def _current_weights(shares: pd.Series, price: pd.Series, equity: float) -> pd.Series:
    weights = pd.Series(0.0, index=shares.index, dtype=float)
    if equity <= 0:
        return weights
    for sym, qty in shares.items():
        px = price[sym]
        if qty != 0 and np.isfinite(px):
            weights[sym] = float(qty) * float(px) / equity
    return weights


def simulate(
    adj_open: pd.DataFrame,
    adj_close: pd.DataFrame,
    raw_open: pd.DataFrame,
    raw_close: pd.DataFrame,
    volumes: pd.DataFrame,
    rebalance_dates: set[pd.Timestamp],
    weight_fn,
    *,
    starting_capital: float = 2_200.0,
    config: StrategyConfig | None = None,
    flatten_weekends: bool = False,
    cost_bps: float = 0.0,
    fee_mode: str = "none",
    sizing_mode: str = "full",
) -> dict:
    """Share ledger on the session index.

    Decisions at a close use prices through that close and the exchange
    calendar (whether a later session exists). The next open is used only to
    fill an already-scheduled re-entry and to measure the gap afterward.
    """
    config = config or StrategyConfig()
    index = pd.DatetimeIndex(adj_close.index).tz_localize(None).normalize()
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError("price index must be sorted and unique")
    week_last = last_session_of_each_week(index)
    symbols = list(adj_close.columns)
    cash = float(starting_capital)
    shares = pd.Series(0.0, index=symbols, dtype=float)
    pending_weights: pd.Series | None = None
    pending_obs: dict | None = None
    daily: list[dict] = []
    weekends: list[dict] = []
    failures: list[dict] = []
    prev_equity_close = float(starting_capital)
    totals = {"slippage": 0.0, "reg": 0.0, "reg_estimated": 0.0, "traded_notional": 0.0, "stuck": 0}
    last_price = pd.Series(np.nan, index=symbols, dtype=float)

    def _px(frame: pd.DataFrame, stamp: pd.Timestamp) -> pd.Series:
        row = frame.loc[stamp]
        return row.reindex(symbols).astype(float)

    def _for_mark(row: pd.Series) -> pd.Series:
        marked = row.copy()
        for sym, px in marked.items():
            if np.isfinite(px):
                last_price[sym] = float(px)
            elif np.isfinite(last_price[sym]):
                marked[sym] = float(last_price[sym])
        return marked

    for i, stamp in enumerate(index):
        stamp = pd.Timestamp(stamp).normalize()
        open_adj = _px(adj_open, stamp)
        close_adj = _px(adj_close, stamp)
        open_raw = _px(raw_open, stamp)
        close_raw = _px(raw_close, stamp)
        slip_today = reg_today = traded_today = 0.0

        if pending_obs is not None:
            gap_pnl = 0.0
            marked_open = _for_mark(open_adj)
            for sym, qty in pending_obs["shares"].items():
                if qty == 0:
                    continue
                nxt = marked_open[sym]
                prev = pending_obs["adj_close"][sym]
                if not np.isfinite(nxt) or not np.isfinite(prev):
                    raise ValueError(f"missing gap price for held {sym} on {stamp.date()}")
                gap_pnl += float(qty) * (float(nxt) - float(prev))
            equity_base = pending_obs["equity_close"]
            pending_obs["next_date"] = stamp
            pending_obs["gap_pnl"] = gap_pnl
            pending_obs["gap_return_on_account"] = gap_pnl / equity_base if equity_base else np.nan
            gross = pending_obs["gross_notional"]
            pending_obs["gap_return_on_gross"] = gap_pnl / gross if gross > 1 else np.nan
            pending_obs.pop("shares", None)
            pending_obs.pop("adj_close", None)
            pending_obs.pop("raw_close", None)
            weekends.append(pending_obs)
            pending_obs = None

        if pending_weights is not None:
            equity_open = mark_to_market(cash, shares, open_adj)
            target = weights_to_shares(pending_weights, equity_open, open_adj, open_raw, config, sizing_mode)
            cash, shares, stats = trade_to(
                cash, shares, target, open_adj, open_raw, cost_bps, fee_schedule(stamp.date(), fee_mode)
            )
            slip_today += stats["slippage"]
            reg_today += stats["reg"]
            traded_today += stats["traded_notional"]
            totals["stuck"] += stats["stuck"]
            totals["reg_estimated"] += stats["reg_estimated"]
            pending_weights = None

        equity_open = mark_to_market(cash, shares, _for_mark(open_adj))
        equity_before = mark_to_market(cash, shares, _for_mark(close_adj))
        intraday = equity_before / equity_open - 1 if equity_open > 0 else 0.0
        from_prior_close = equity_before / prev_equity_close - 1 if prev_equity_close > 0 else 0.0

        signal = None
        if stamp in rebalance_dates and i < len(index) - 1:
            hist_adj = adj_close.loc[:stamp]
            hist_raw = raw_close.loc[:stamp]
            hist_vol = volumes.loc[:stamp]
            try:
                signal = weight_fn(stamp, hist_adj, hist_raw, hist_vol, equity_before)
            except ValueError as exc:
                failures.append({"date": str(stamp.date()), "error": str(exc)})
                signal = None
            if signal is not None:
                signal = signal.reindex(symbols).fillna(0.0).astype(float)

        is_week_last = stamp in week_last and i < len(index) - 1
        if flatten_weekends and is_week_last:
            if signal is None:
                pending_weights = _current_weights(shares, close_adj, equity_before)
            else:
                pending_weights = signal
            zero = pd.Series(0.0, index=symbols)
            cash, shares, stats = trade_to(
                cash, shares, zero, close_adj, close_raw, cost_bps, fee_schedule(stamp.date(), fee_mode)
            )
            slip_today += stats["slippage"]
            reg_today += stats["reg"]
            traded_today += stats["traded_notional"]
            totals["stuck"] += stats["stuck"]
            totals["reg_estimated"] += stats["reg_estimated"]
        elif signal is not None:
            target = weights_to_shares(signal, equity_before, close_adj, close_raw, config, sizing_mode)
            cash, shares, stats = trade_to(
                cash, shares, target, close_adj, close_raw, cost_bps, fee_schedule(stamp.date(), fee_mode)
            )
            slip_today += stats["slippage"]
            reg_today += stats["reg"]
            traded_today += stats["traded_notional"]
            totals["stuck"] += stats["stuck"]
            totals["reg_estimated"] += stats["reg_estimated"]

        marked_close = _for_mark(close_adj)
        equity_close = mark_to_market(cash, shares, marked_close)
        gross = float((shares.abs() * marked_close).sum(skipna=True))
        net = float((shares * marked_close).sum(skipna=True))
        n_long = int((shares > 1e-10).sum())
        n_short = int((shares < -1e-10).sum())
        daily.append({
            "date": stamp,
            "equity_open": equity_open,
            "equity_close_before": equity_before,
            "equity_close": equity_close,
            "gross_notional": gross,
            "net_notional": net,
            "gross_weight": gross / equity_close if equity_close else np.nan,
            "net_weight": net / equity_close if equity_close else np.nan,
            "n_long": n_long,
            "n_short": n_short,
            "slippage": slip_today,
            "reg": reg_today,
            "traded_notional": traded_today,
            "intraday_return": intraday,
            "close_to_close_before_trades": from_prior_close,
        })
        totals["slippage"] += slip_today
        totals["reg"] += reg_today
        totals["traded_notional"] += traded_today

        if is_week_last:
            # Shares actually carried overnight (zero when flattened). Measurement
            # of the next open happens on the next iteration, before any re-entry.
            pending_obs = {
                "date": stamp,
                "next_date": None,
                "shares": shares.copy(),
                "adj_close": close_adj.copy(),
                "raw_close": close_raw.copy(),
                "equity_close": equity_close,
                "gross_notional": gross,
                "net_notional": net,
                "friday_intraday": intraday,
                "prior_close_to_last_close": from_prior_close,
                "cash": cash,
            }
        prev_equity_close = equity_close

    daily_df = pd.DataFrame(daily).set_index("date")
    weekend_df = pd.DataFrame(weekends)
    if len(weekend_df):
        # Monday open-to-close, after the open's re-entry trade.
        intraday_by_day = daily_df["intraday_return"]
        weekend_df["monday_intraday"] = weekend_df["next_date"].map(intraday_by_day)
    invested = daily_df.index[daily_df["gross_notional"] > 1.0]
    first = pd.Timestamp(invested[0]) if len(invested) else None
    last = pd.Timestamp(invested[-1]) if len(invested) else None
    return {
        "daily": daily_df,
        "weekends": weekend_df,
        "failures": failures,
        "totals": totals,
        "first_invested": first,
        "last_invested": last,
        "starting_capital": starting_capital,
        "ending_equity": float(daily_df["equity_close"].iloc[-1]) if len(daily_df) else starting_capital,
    }


def slice_metrics(daily: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    sub = daily.loc[(daily.index >= start) & (daily.index <= end)]
    if len(sub) < 2:
        return {"available": False, "n_sessions": int(len(sub))}
    eq = sub["equity_close"].astype(float)
    eq0 = float(eq.iloc[0])
    eq1 = float(eq.iloc[-1])
    rets = eq.pct_change().iloc[1:]
    points = [eq0]
    for stamp, row in sub.iloc[1:].iterrows():
        points.append(float(row.equity_open))
        points.append(float(row.equity_close))
    arr = np.asarray(points, dtype=float)
    peak = np.maximum.accumulate(arr)
    drawdown = arr / peak - 1
    n_sessions = int(len(sub))
    years = (sub.index[-1] - sub.index[0]).days / 365.25
    annualize = n_sessions >= 252 and years >= 1
    std = float(rets.std(ddof=1)) if len(rets) > 1 else np.nan
    mean = float(rets.mean()) if len(rets) else np.nan
    traded = float(sub["traded_notional"].sum())
    return {
        "available": True,
        "start": str(sub.index[0].date()),
        "end": str(sub.index[-1].date()),
        "n_sessions": n_sessions,
        "ending_equity": eq1,
        "total_return": eq1 / eq0 - 1 if eq0 else np.nan,
        "cagr": (eq1 / eq0) ** (1 / years) - 1 if annualize and eq0 > 0 else None,
        "annualization_ok": annualize,
        "sharpe_zero_rf": (mean / std * np.sqrt(252)) if annualize and std and np.isfinite(std) and std > 0 else None,
        "annual_volatility": (std * np.sqrt(252)) if annualize and np.isfinite(std) else None,
        "max_drawdown": float(drawdown.min()),
        "mean_gross_weight": float(sub["gross_weight"].mean()),
        "mean_net_weight": float(sub["net_weight"].mean()),
        "traded_notional": traded,
        "traded_notional_over_start_equity": traded / eq0 if eq0 else np.nan,
        "slippage_dollars": float(sub["slippage"].sum()),
        "reg_dollars": float(sub["reg"].sum()),
        "days_below_starting_note": None,
    }


def distribution(values: pd.Series | np.ndarray) -> dict:
    x = np.asarray(pd.Series(values).dropna(), dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return {"n": 0}
    std = float(x.std(ddof=1)) if len(x) > 1 else np.nan
    mean = float(x.mean())
    t_stat = None
    if len(x) > 1 and std > 0:
        t_stat = mean / (std / math.sqrt(len(x)))
    return {
        "n": int(len(x)),
        "mean": mean,
        "median": float(np.median(x)),
        "std": std,
        "t_stat_vs_zero": t_stat,
        "pct_negative": float(np.mean(x < 0)),
        "pct_positive": float(np.mean(x > 0)),
        "p05": float(np.quantile(x, 0.05)),
        "p95": float(np.quantile(x, 0.95)),
        "sum": float(x.sum()),
        "worst": float(x.min()),
        "best": float(x.max()),
    }


def make_ls_weight_fn(config: StrategyConfig):
    """Point-in-time ``calculate_targets``. Cached by date; independent of the account path."""
    cache: dict[pd.Timestamp, pd.Series | None] = {}

    def fn(date, adj_hist, raw_hist, vol_hist, equity):  # noqa: ARG001 equity is sizing, not the signal
        date = pd.Timestamp(date).normalize()
        if date in cache:
            cached = cache[date]
            return None if cached is None else cached.copy()
        if len(adj_hist) < config.momentum_lookback + 1:
            cache[date] = None
            return None
        scaled = point_in_time_closes(adj_hist, raw_hist)
        vols = vol_hist.reindex(index=scaled.index, columns=scaled.columns).fillna(0.0)
        try:
            targets = calculate_targets(scaled, vols, config)
        except ValueError as exc:
            fn.errors.append({"date": str(date.date()), "error": str(exc)})
            cache[date] = None
            return None
        weights = targets["target_weight"].astype(float)
        cache[date] = weights
        return weights.copy()

    fn.cache = cache
    fn.errors = []
    return fn


def make_fixed_weight_fn(weights: dict[str, float]):
    series = pd.Series(weights, dtype=float)

    def fn(date, adj_hist, raw_hist, vol_hist, equity):  # noqa: ARG001
        if adj_hist.index.max() > pd.Timestamp(date):
            raise RuntimeError("weight function received prices after the decision date")
        return series.copy()

    return fn


def json_ready(value):
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if value is pd.NaT:
        return None
    return value


def _yahoo_chart(symbol: str, start: datetime, end: datetime) -> dict:
    query = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?period1={int(start.timestamp())}&period2={int(end.timestamp())}"
        "&interval=1d&events=div%7Csplits&includeAdjustedClose=true"
    )
    request = urllib.request.Request(query, headers={"User-Agent": "Mozilla/5.0 weekend-flat-research"})
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.load(response)
            result = payload["chart"]["result"]
            if not result:
                raise RuntimeError(payload["chart"].get("error"))
            return result[0]
        except Exception as exc:  # noqa: BLE001 — retry network/API failures
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last_error}")


def _session_frame(chart: dict) -> pd.DataFrame:
    timestamps = chart.get("timestamp") or []
    quote = chart["indicators"]["quote"][0]
    adj = chart["indicators"]["adjclose"][0]["adjclose"]
    rows = []
    for i, ts in enumerate(timestamps):
        session = datetime.fromtimestamp(ts, timezone.utc).astimezone(ET).date()
        rows.append({
            "date": pd.Timestamp(session),
            "raw_open": quote["open"][i],
            "raw_close": quote["close"][i],
            "adj_close": adj[i],
            "volume": quote["volume"][i],
        })
    frame = pd.DataFrame(rows).dropna(subset=["raw_close", "raw_open", "adj_close"])
    frame = frame.drop_duplicates("date", keep="last").set_index("date").sort_index()
    factor = frame["adj_close"] / frame["raw_close"].replace(0, np.nan)
    frame["adj_open"] = frame["raw_open"] * factor
    events = chart.get("events") or {}
    frame.attrs["n_dividends"] = len(events.get("dividends") or {})
    frame.attrs["n_splits"] = len(events.get("splits") or {})
    return frame


def load_prices(symbols: list[str], refresh: bool = False) -> tuple[dict[str, pd.DataFrame], dict]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    cache = OUTPUT / "prices.parquet"
    meta_path = OUTPUT / "price_meta.json"
    if cache.exists() and meta_path.exists() and not refresh:
        wide = pd.read_parquet(cache)
        meta = json.loads(meta_path.read_text())
        panels = {field: wide[field] for field in ("raw_open", "raw_close", "adj_open", "adj_close", "volume")}
        return panels, meta

    start = datetime(2004, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 11, 9, tzinfo=timezone.utc)
    frames = []
    meta_symbols = {}
    for symbol in symbols:
        chart = _yahoo_chart(symbol, start, end)
        frame = _session_frame(chart)
        frame.columns = pd.MultiIndex.from_product([frame.columns, [symbol]])
        frames.append(frame)
        meta_symbols[symbol] = {
            "rows": int(len(frame)),
            "start": str(frame.index[0].date()) if len(frame) else None,
            "end": str(frame.index[-1].date()) if len(frame) else None,
            "n_dividends": int(frame.attrs["n_dividends"]),
            "n_splits": int(frame.attrs["n_splits"]),
        }
        time.sleep(0.05)
    wide = pd.concat(frames, axis=1).sort_index()
    wide.to_parquet(cache)
    meta = {
        "source": "Yahoo Finance chart API v8, interval=1d, includeAdjustedClose=true",
        "url_template": "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "request_start": "2004-01-01",
        "request_end_exclusive": "2026-11-09",
        "symbols": meta_symbols,
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    panels = {
        field: wide[field].copy()
        for field in ("raw_open", "raw_close", "adj_open", "adj_close", "volume")
    }
    return panels, meta


def _ffill_limit(frame: pd.DataFrame, limit: int = 3) -> tuple[pd.DataFrame, int]:
    filled = frame.ffill(limit=limit)
    # Count only values that were missing and are now present, ignoring leading NaNs.
    introduced = int((frame.isna() & filled.notna()).sum().sum())
    return filled, introduced


def _align(panels: dict[str, pd.DataFrame], symbols: list[str], calendar: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    out = {}
    for key, frame in panels.items():
        out[key] = frame.reindex(index=calendar, columns=symbols)
    return out


def _prep_book(panels: dict[str, pd.DataFrame], symbols: list[str], calendar: pd.DatetimeIndex) -> tuple[dict[str, pd.DataFrame], int]:
    aligned = _align(panels, symbols, calendar)
    introduced = 0
    for key in ("raw_open", "raw_close", "adj_open", "adj_close"):
        aligned[key], n = _ffill_limit(aligned[key], 3)
        introduced += n
    aligned["volume"] = aligned["volume"].fillna(0.0)
    return aligned, introduced


def dividend_example(raw_close: pd.Series, adj_close: pd.Series) -> dict | None:
    factor = (adj_close / raw_close.replace(0, np.nan)).dropna()
    change = factor.pct_change().abs()
    jumps = change[change > 0.001]
    if jumps.empty:
        return None
    # Prefer a jump whose previous session falls on Friday: a weekend ex-date.
    chosen = None
    for stamp in jumps.index:
        loc = factor.index.get_loc(stamp)
        if loc == 0:
            continue
        prev = factor.index[loc - 1]
        if prev.weekday() == 4:
            chosen = stamp
            break
    if chosen is None:
        chosen = jumps.index[0]
        loc = factor.index.get_loc(chosen)
        prev = factor.index[loc - 1]
    raw_gap = float(raw_close.loc[chosen] / raw_close.loc[prev] - 1)
    adj_gap = float(adj_close.loc[chosen] / adj_close.loc[prev] - 1)
    return {
        "symbol": str(raw_close.name),
        "previous_session": str(prev.date()),
        "session": str(pd.Timestamp(chosen).date()),
        "previous_weekday": int(prev.weekday()),
        "raw_close_to_close": raw_gap,
        "adjusted_close_to_close": adj_gap,
        "note": "Adjusted return puts the dividend back into the ex-date move. Raw close-to-close omits the cash dividend.",
    }


def run_book(name: str, panels: dict[str, pd.DataFrame], calendar, weight_fn, rebalance, config, sizing_mode, flatten, bps, fee_mode, capital):
    result = simulate(
        panels["adj_open"], panels["adj_close"], panels["raw_open"], panels["raw_close"], panels["volume"],
        rebalance, weight_fn,
        starting_capital=capital, config=config, flatten_weekends=flatten,
        cost_bps=bps, fee_mode=fee_mode, sizing_mode=sizing_mode,
    )
    daily = result["daily"]
    if result["first_invested"] is None:
        slices = {}
    else:
        start = result["first_invested"]
        slices = {
            "full": slice_metrics(daily, start, daily.index[-1]),
            "pre_holdout": slice_metrics(daily, start, pd.Timestamp("2022-12-31")),
            "holdout": slice_metrics(daily, max(start, HOLDOUT_START), HOLDOUT_END),
            "live": slice_metrics(daily, LIVE_START, daily.index[-1]),
        }
    invested = daily.loc[result["first_invested"]:] if result["first_invested"] is not None else daily.iloc[0:0]
    summary = {
        "book": name,
        "flatten_weekends": flatten,
        "cost_bps_per_side": bps,
        "fee_mode": fee_mode,
        "sizing_mode": sizing_mode,
        "first_invested": None if result["first_invested"] is None else str(result["first_invested"].date()),
        "last_invested": None if result["last_invested"] is None else str(result["last_invested"].date()),
        "ending_equity": result["ending_equity"],
        "starting_capital": capital,
        "n_signal_failures": len(result["failures"]),
        "signal_failure_dates_tail": [row["date"] for row in result["failures"][-12:]],
        "mean_gross_weight_while_invested": float(invested["gross_weight"].mean()) if len(invested) else None,
        "mean_net_weight_while_invested": float(invested["net_weight"].mean()) if len(invested) else None,
        "mean_n_long": float(invested["n_long"].mean()) if len(invested) else None,
        "mean_n_short": float(invested["n_short"].mean()) if len(invested) else None,
        "fraction_days_gross_below_1pct": float((invested["gross_weight"] < 0.01).mean()) if len(invested) else None,
        "totals": result["totals"],
        "slices": slices,
    }
    return result, summary


def weekend_stats(result: dict, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> dict:
    frame = result["weekends"]
    if frame is None or len(frame) == 0:
        return {"n": 0}
    view = frame.dropna(subset=["next_date"]).copy()
    if "gross_notional" in view.columns:
        view = view[view["gross_notional"] > 1.0]
    if start is not None:
        view = view[view["date"] >= pd.Timestamp(start)]
    if end is not None:
        view = view[view["next_date"] <= pd.Timestamp(end)]
    return {
        "gap_return_on_account": distribution(view["gap_return_on_account"]),
        "gap_return_on_gross": distribution(view["gap_return_on_gross"]),
        "last_session_intraday": distribution(view["friday_intraday"]),
        "prior_close_to_last_close": distribution(view["prior_close_to_last_close"]),
        "next_session_intraday": distribution(view["monday_intraday"]),
    }


def save_figures(out: Path, curves: dict[str, pd.Series], weekends: dict[str, pd.DataFrame], sensitivity: dict) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    paths = []
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4), sharey=False)
    for ax, key, title in (
        (axes[0], "spy", "SPY control, fully invested"),
        (axes[1], "ls", "Repo long/short, not A242"),
    ):
        for label, series in curves[key].items():
            base = series.dropna()
            if base.empty:
                continue
            norm = base / float(base.iloc[0])
            ax.plot(norm.index, norm.values, label=label, linewidth=1.3)
        ax.set_title(title)
        ax.set_ylabel("Growth of $1 at first invested close")
        ax.legend(frameon=False, fontsize=8)
        ax.grid(True, alpha=0.3)
    fig.suptitle("Hold through the weekend vs flatten at the last session (5 bps per side, no regulatory fee)")
    fig.tight_layout()
    path = out / "equity_normalized_5bps.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    paths.append(path.name)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    for ax, key, title in (
        (axes[0], "spy", "SPY last close → next open"),
        (axes[1], "ls", "Repo long/short last close → next open"),
    ):
        held = weekends[key]
        held = held[held["gross_notional"] > 1.0]
        values = held["gap_return_on_account"].dropna() * 10_000
        ax.hist(values, bins=40, color="#3c5a73", edgecolor="white")
        ax.axvline(0, color="black", linewidth=0.8)
        ax.axvline(values.mean(), color="#9a3412", linewidth=1.2, label=f"mean {values.mean():.1f} bps")
        ax.set_title(title)
        ax.set_xlabel("Account return over the gap (bps)")
        ax.legend(frameon=False, fontsize=8)
    fig.suptitle("Completed weekend/holiday gaps on the hold book at 0 bps (unfinished final week excluded)")
    fig.tight_layout()
    path = out / "weekend_gap_histograms.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    paths.append(path.name)

    fig, ax = plt.subplots(figsize=(9.5, 4.4))
    labels = ["Prior close → last close", "Last session open → close", "Last close → next open", "Next open → next close"]
    fields = ["prior_close_to_last_close", "friday_intraday", "gap_return_on_account", "monday_intraday"]
    x = np.arange(len(labels))
    width = 0.36
    for offset, key, name in ((-width / 2, "spy", "SPY"), (width / 2, "ls", "Repo long/short")):
        held = weekends[key]
        held = held[held["gross_notional"] > 1.0]
        means = [float(held[field].mean()) * 10_000 for field in fields]
        ax.bar(x + offset, means, width=width, label=name)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xticks(x, labels, rotation=12, ha="right")
    ax.set_ylabel("Mean return (bps)")
    ax.set_title("Where the move sits: Friday session vs the overnight gap vs the next session")
    ax.legend(frameon=False)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    path = out / "return_components.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    paths.append(path.name)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=False)
    for ax, key, title in (
        (axes[0], "spy", "SPY"),
        (axes[1], "ls", "Repo long/short (not A242)"),
    ):
        table = sensitivity[key]
        bps = [row["bps"] for row in table]
        xpos = np.arange(len(bps))
        ax.bar(xpos - 0.18, [row["hold"] * 100 for row in table], width=0.36, label="Hold")
        ax.bar(xpos + 0.18, [row["flat"] * 100 for row in table], width=0.36, label="Flatten")
        ax.set_xticks(xpos, [f"{b} bps" for b in bps])
        ax.set_ylabel("Full-sample total return (%)")
        ax.set_title(title)
        ax.legend(frameon=False, fontsize=8)
        ax.grid(True, axis="y", alpha=0.3)
    fig.suptitle("Cost sensitivity, per side, regulatory fees off")
    fig.tight_layout()
    path = out / "cost_sensitivity.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    paths.append(path.name)
    return paths


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def short_cap_diagnostic(raw_close: pd.DataFrame, config: StrategyConfig) -> dict:
    cap = config.starting_capital * config.max_position_weight
    under = raw_close.le(cap) & raw_close.notna()
    by_year = {}
    for year, frame in under.groupby(under.index.year):
        last = frame.iloc[-1]
        by_year[str(int(year))] = {
            "names_at_or_under_cap": int(last.sum()),
            "names_priced": int(last.notna().sum()) if False else int(raw_close.loc[frame.index[-1]].notna().sum()),
        }
    last_row = raw_close.iloc[-1]
    return {
        "short_price_cap": cap,
        "rule": "calculate_targets skips a short when the tradeable price exceeds starting_capital * max_position_weight ($220 at defaults).",
        "names_under_cap_on_last_session": sorted(last_row[last_row.le(cap)].index.tolist()),
        "names_over_cap_on_last_session": sorted(last_row[last_row.gt(cap)].index.tolist()),
        "by_year_end": by_year,
    }


def main(refresh: bool = False) -> None:
    config = StrategyConfig()
    symbols = list(dict.fromkeys([*config.symbols, "SPY", *SNAPSHOT_SYMBOLS]))
    panels, price_meta = load_prices(symbols, refresh=refresh)
    calendar = panels["raw_close"]["SPY"].dropna().index
    calendar = pd.DatetimeIndex(calendar).tz_localize(None).normalize()
    calendar = calendar[~calendar.duplicated()].sort_values()
    calendar_checks = validate_session_calendar(calendar)
    introduced = {}
    books = {}
    for name, cols in (
        ("ls", [s for s in config.symbols if s in panels["adj_close"].columns]),
        ("spy", ["SPY"]),
        ("snapshot", [s for s in SNAPSHOT_SYMBOLS if s in panels["adj_close"].columns]),
    ):
        books[name], introduced[name] = _prep_book(panels, cols, calendar)

    rebalance = month_end_sessions(calendar)
    # The final session has no next print. Skipping it matches run_backtest,
    # which does not charge a rebalance it cannot evaluate.
    final = pd.Timestamp(calendar[-1]).normalize()
    rebalance.discard(final)

    ls_weights = make_ls_weight_fn(config)
    spy_weights = make_fixed_weight_fn({"SPY": 1.0})
    snap_weights = make_fixed_weight_fn({sym: 1 / len(books["snapshot"]["adj_close"].columns) for sym in books["snapshot"]["adj_close"].columns})

    specs = {
        "ls": (books["ls"], ls_weights, "live_cap"),
        "spy": (books["spy"], spy_weights, "full"),
        "snapshot": (books["snapshot"], snap_weights, "full"),
    }
    summaries = {}
    results = {}
    grid = [(bps, "none") for bps in (0, 5, 10)] + [(5, "disclosed")]
    for book, (panel, weight_fn, sizing) in specs.items():
        for flatten in (False, True):
            for bps, fee_mode in grid:
                key = f"{book}_{'flat' if flatten else 'hold'}_bps{bps}_fee{fee_mode}"
                result, summary = run_book(
                    book, panel, calendar, weight_fn, rebalance, config, sizing,
                    flatten, bps, fee_mode, config.starting_capital,
                )
                results[key] = result
                summaries[key] = summary
                print(
                    f"{key}: end {summary['ending_equity']:.2f} "
                    f"full {summary['slices'].get('full', {}).get('total_return')} "
                    f"failures {summary['n_signal_failures']}",
                    flush=True,
                )

    # Weekend distributions come from the zero-cost hold book so the mark
    # move is not mixed with the flatten policy's costs.
    weekend_payload = {}
    windows = {
        "full": (None, None),
        "pre_holdout": (None, pd.Timestamp("2022-12-31")),
        "holdout": (HOLDOUT_START, HOLDOUT_END),
        "live": (LIVE_START, calendar[-1]),
    }
    for book in ("ls", "spy", "snapshot"):
        hold = results[f"{book}_hold_bps0_feenone"]
        weekend_payload[book] = {
            label: weekend_stats(hold, start, end)
            for label, (start, end) in windows.items()
        }
        if book != "snapshot":
            hold["weekends"].to_parquet(OUTPUT / f"weekends_{book}.parquet")

    sensitivity = {}
    for book in ("ls", "spy", "snapshot"):
        rows = []
        for bps in (0, 5, 10):
            hold = summaries[f"{book}_hold_bps{bps}_feenone"]["slices"]["full"]["total_return"]
            flat = summaries[f"{book}_flat_bps{bps}_feenone"]["slices"]["full"]["total_return"]
            rows.append({"bps": bps, "hold": hold, "flat": flat, "flat_minus_hold": flat - hold})
        sensitivity[book] = rows

    curves = {}
    for book in ("spy", "ls"):
        curves[book] = {}
        for label, key in (
            ("Hold", f"{book}_hold_bps5_feenone"),
            ("Flatten", f"{book}_flat_bps5_feenone"),
        ):
            daily = results[key]["daily"]["equity_close"]
            first = results[key]["first_invested"]
            curves[book][label] = daily.loc[first:]
    weekend_frames = {
        "spy": results["spy_hold_bps0_feenone"]["weekends"],
        "ls": results["ls_hold_bps0_feenone"]["weekends"],
    }
    figures = save_figures(OUTPUT, curves, weekend_frames, sensitivity)

    example = dividend_example(
        books["spy"]["raw_close"]["SPY"].rename("SPY"),
        books["spy"]["adj_close"]["SPY"].rename("SPY"),
    )
    cap_info = short_cap_diagnostic(books["ls"]["raw_close"], config)
    # Signal cache is path-independent. Record how many month-ends produced a book.
    cache = ls_weights.cache
    n_targets = sum(value is not None for value in cache.values())
    n_none = sum(value is None for value in cache.values())
    payload = {
        "identity": {
            "a242_in_repo": False,
            "label": "NOT A242. In-repo proxy is wallstreet_ls.strategy.calculate_targets long/short.",
            "repo_strategy_version": "wallstreet-ls 0.1.0",
            "git_head": _git_head(),
            "config": {
                "starting_capital": config.starting_capital,
                "long_count": config.long_count,
                "short_count": config.short_count,
                "momentum_lookback": config.momentum_lookback,
                "momentum_skip": config.momentum_skip,
                "volatility_lookback": config.volatility_lookback,
                "gross_exposure": config.gross_exposure,
                "max_position_weight": config.max_position_weight,
                "target_annual_volatility": config.target_annual_volatility,
                "universe": list(config.symbols),
            },
            "live_scripts_call": "python -m wallstreet_ls.cli --strategy long-short",
            "current_snapshot_holdings_are_not_the_historical_book": list(SNAPSHOT_SYMBOLS),
        },
        "data": {
            "price_meta": price_meta,
            "calendar": calendar_checks,
            "first_session": str(calendar[0].date()),
            "last_session": str(calendar[-1].date()),
            "tested_through": str(calendar[-1].date()),
            "not_tested_through": "2026-11-08 is a Sunday. The Nov 7-9 weekend has no Monday open in the data. The final Friday is marked, not flattened.",
            "ffill_limit_3_cells": introduced,
            "adjustment": "Signals use point-in-time rescaled adjusted closes (last row = raw close). Marks and gap P&L use one adjusted open/close factor, adj_open = raw_open * adj_close/raw_close, so dividends and splits hit open and close consistently.",
            "dividend_example": example,
            "short_cap": cap_info,
        "signal_cache_month_ends_with_targets": n_targets,
        "signal_cache_month_ends_without_targets": n_none,
        "ls_target_errors": ls_weights.errors,
    },
        "execution_assumptions": {
            "hold": "Month-end signal from calculate_targets trades at that session's close. Shares then stay fixed until the next month-end. Close-to-next-open gaps, including weekends and holidays, are marked.",
            "flat": "On the last session of each week, sell the book at that close and buy the same target weights at the next session's open, sized off equity at the open. A month-end that is also the week's last session defers the new book to the next open instead of trading twice.",
            "slippage": "0, 5, and 10 bps PER SIDE versus the official open or close print. No NBBO, no intraday path, so trading a few minutes early is not separately identified.",
            "regulatory": "Primary grid charges none. The disclosed schedule is a separate 5 bps run. Alpaca equity commission modeled as zero. No penny rounding. CAT fee omitted.",
            "cash_interest": "Zero. Flat cash earns no T-bill interest. This leaves out a benefit of flattening a net-long book.",
            "borrow": "Zero. Holding shorts over the weekend is not charged borrow. This favors the hold policy if borrow is positive.",
            "shorts": "Fractional economically via adjusted units, but the share count is an integer number of tradeable shares, matching the live short rounder.",
            "sizing": "Long/short uses the live cap min(equity, $2,200). SPY and the four-name snapshot compound on full equity.",
        },
        "slices_predeclared": {
            "full": "first invested close through last session",
            "pre_holdout": "first invested close through 2022-12-31",
            "holdout": "2023-01-01 through 2026-10-07",
            "live": "2026-10-08 through last session",
            "note": "No weekday parameter was chosen from these windows.",
        },
        "summaries": summaries,
        "weekend_distributions_hold_0bps": weekend_payload,
        "cost_sensitivity_full_sample": sensitivity,
        "figures": figures,
    }
    (OUTPUT / "results.json").write_text(json.dumps(json_ready(payload), indent=2))
    print(f"Wrote {OUTPUT / 'results.json'}", flush=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Research-only weekend hold vs flatten study")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    main(refresh=args.refresh)
