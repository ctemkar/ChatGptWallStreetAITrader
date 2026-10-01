from __future__ import annotations

import numpy as np
import pandas as pd


def run_quick_flip(
    bars: pd.DataFrame,
    starting_capital: float = 2_200.0,
    risk_fraction: float = 0.005,
    reward_risk: float = 1.5,
    slippage_bps: float = 2.0,
) -> tuple[pd.DataFrame, dict]:
    """Mechanical interpretation of the video's Quick Flip Scalper on 5m bars."""
    bars = bars.sort_index().copy()
    if bars.index.tz is None:
        raise ValueError("bars must have a timezone-aware index")
    local = bars.tz_convert("America/New_York").between_time("09:30", "11:00")
    equity = starting_capital
    trades: list[dict] = []
    for day, frame in local.groupby(local.index.date):
        if len(frame) < 10:
            continue
        opening = frame.iloc[:3]
        opening_high, opening_low = opening.high.max(), opening.low.min()
        prior_range = (frame.high - frame.low).rolling(14, min_periods=3).mean()
        sweep_idx = None
        direction = None
        for i in range(3, min(18, len(frame))):
            candle = frame.iloc[i]
            significant = candle.high - candle.low >= prior_range.iloc[i]
            if significant and candle.low < opening_low and candle.close > opening_low:
                sweep_idx, direction = i, 1
                break
            if significant and candle.high > opening_high and candle.close < opening_high:
                sweep_idx, direction = i, -1
                break
        if sweep_idx is None:
            continue
        signal_idx = None
        for i in range(sweep_idx + 1, min(sweep_idx + 7, len(frame))):
            cur, prev = frame.iloc[i], frame.iloc[i - 1]
            body = max(abs(cur.close - cur.open), cur.close * 0.00005)
            lower_wick = min(cur.open, cur.close) - cur.low
            upper_wick = cur.high - max(cur.open, cur.close)
            bullish = (cur.close > cur.open and lower_wick >= 2 * body) or (
                cur.close > prev.open and cur.open < prev.close and prev.close < prev.open
            )
            bearish = (cur.close < cur.open and upper_wick >= 2 * body) or (
                cur.open > prev.close and cur.close < prev.open and prev.close > prev.open
            )
            if (direction == 1 and bullish) or (direction == -1 and bearish):
                signal_idx = i
                break
        if signal_idx is None:
            continue
        signal = frame.iloc[signal_idx]
        slip = slippage_bps / 10_000
        entry = signal.close * (1 + direction * slip)
        stop = signal.low * (1 - slip) if direction == 1 else signal.high * (1 + slip)
        unit_risk = abs(entry - stop)
        if unit_risk <= 0:
            continue
        target = entry + direction * reward_risk * unit_risk
        quantity = equity * risk_fraction / unit_risk
        exit_price, outcome = frame.iloc[-1].close, "time"
        for _, candle in frame.iloc[signal_idx + 1 :].iterrows():
            stop_hit = candle.low <= stop if direction == 1 else candle.high >= stop
            target_hit = candle.high >= target if direction == 1 else candle.low <= target
            if stop_hit:
                exit_price, outcome = stop, "stop"
                break
            if target_hit:
                exit_price, outcome = target, "target"
                break
        exit_price *= 1 - direction * slip
        pnl = direction * quantity * (exit_price - entry)
        equity += pnl
        trades.append({"date": str(day), "direction": direction, "entry": entry, "exit": exit_price, "pnl": pnl, "outcome": outcome, "equity": equity})
    result = pd.DataFrame(trades)
    if result.empty:
        return result, {"trades": 0, "ending_equity": starting_capital, "total_return": 0.0}
    peak = result.equity.cummax().clip(lower=starting_capital)
    metrics = {
        "trades": len(result),
        "ending_equity": float(equity),
        "total_return": float(equity / starting_capital - 1),
        "win_rate": float((result.pnl > 0).mean()),
        "profit_factor": float(result.loc[result.pnl > 0, "pnl"].sum() / -result.loc[result.pnl < 0, "pnl"].sum()) if (result.pnl < 0).any() else np.inf,
        "max_drawdown": float((result.equity / peak - 1).min()),
        "average_trade": float(result.pnl.mean()),
    }
    return result, metrics


def run_sneaky_pivot(
    bars: pd.DataFrame,
    starting_capital: float = 2_200.0,
    risk_fraction: float = 0.005,
    reward_risk: float = 1.5,
    slippage_bps: float = 2.0,
) -> tuple[pd.DataFrame, dict]:
    """Conservative proxy for The Rumers' 15-minute ``sneaky pivot`` setup.

    Uses prior-day high/low and the prior five-session swing high/low. A trade
    requires a touch of one of the four levels followed by a confirming close
    back through the preceding candle; stop is beyond the level and target is
    1.5R. The video leaves these details subjective, so results are only a
    rule-based approximation.
    """
    bars = bars.sort_index().copy()
    if bars.index.tz is None:
        raise ValueError("bars must have a timezone-aware index")
    local = bars.tz_convert("America/New_York").between_time("09:30", "16:00")
    daily = local.resample("1D").agg({"high": "max", "low": "min"}).dropna()
    equity, trades = starting_capital, []
    slip = slippage_bps / 10_000
    for day, frame in local.groupby(local.index.date):
        prior = daily.loc[daily.index.date < day]
        if len(prior) < 6 or len(frame) < 3:
            continue
        prev, history = prior.iloc[-1], prior.iloc[-6:-1]
        levels = {"high": float(prev.high), "low": float(prev.low),
                  "swing_high": float(history.high.max()), "swing_low": float(history.low.min())}
        for i in range(1, len(frame) - 1):
            candle, nxt = frame.iloc[i], frame.iloc[i + 1]
            direction = None; level = None
            for name in ("low", "swing_low", "high", "swing_high"):
                value = levels[name]
                if candle.low <= value <= candle.high:
                    if name in ("low", "swing_low") and nxt.close > candle.high:
                        direction, level = 1, value
                    elif name in ("high", "swing_high") and nxt.close < candle.low:
                        direction, level = -1, value
                    if direction is not None:
                        break
            if direction is None:
                continue
            entry = float(nxt.close * (1 + direction * slip))
            stop = float(level * (1 - slip)) if direction == 1 else float(level * (1 + slip))
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            target = entry + direction * reward_risk * risk
            qty = equity * risk_fraction / risk
            exit_price, outcome = float(frame.iloc[-1].close), "time"
            for _, bar in frame.iloc[i + 2:].iterrows():
                if (direction == 1 and bar.low <= stop) or (direction == -1 and bar.high >= stop):
                    exit_price, outcome = stop, "stop"; break
                if (direction == 1 and bar.high >= target) or (direction == -1 and bar.low <= target):
                    exit_price, outcome = target, "target"; break
            exit_price *= 1 - direction * slip
            pnl = direction * qty * (exit_price - entry); equity += pnl
            trades.append({"date": str(day), "direction": direction, "level": level, "entry": entry,
                           "exit": exit_price, "pnl": pnl, "outcome": outcome, "equity": equity})
            break
    result = pd.DataFrame(trades)
    if result.empty:
        return result, {"trades": 0, "ending_equity": starting_capital, "total_return": 0.0}
    peak = result.equity.cummax().clip(lower=starting_capital)
    metrics = {"trades": len(result), "ending_equity": float(equity),
               "total_return": float(equity / starting_capital - 1),
               "win_rate": float((result.pnl > 0).mean()),
               "profit_factor": float(result.loc[result.pnl > 0, "pnl"].sum() / -result.loc[result.pnl < 0, "pnl"].sum()) if (result.pnl < 0).any() else np.inf,
               "max_drawdown": float((result.equity / peak - 1).min()),
               "average_trade": float(result.pnl.mean())}
    return result, metrics


def run_prior_day_breakout(
    bars: pd.DataFrame,
    starting_capital: float = 2_200.0,
    risk_fraction: float = 0.005,
    reward_risk: float = 1.5,
    slippage_bps: float = 2.0,
) -> tuple[pd.DataFrame, dict]:
    """Prior-day high/low breakout proxy on intraday bars."""
    bars = bars.sort_index().copy()
    if bars.index.tz is None:
        raise ValueError("bars must have a timezone-aware index")
    local = bars.tz_convert("America/New_York").between_time("09:30", "16:00")
    daily = local.resample("1D").agg({"high": "max", "low": "min"}).dropna()
    equity, trades, slip = starting_capital, [], slippage_bps / 10_000
    for day, frame in local.groupby(local.index.date):
        prior = daily.loc[daily.index.date < day]
        if prior.empty or len(frame) < 3:
            continue
        prev = prior.iloc[-1]; high, low = float(prev.high), float(prev.low)
        rng = high - low
        if rng <= 0: continue
        direction = None; idx = None
        for i in range(1, len(frame)):
            c, p = frame.iloc[i], frame.iloc[i - 1]
            if c.close > high and p.close <= high:
                direction, idx = 1, i; break
            if c.close < low and p.close >= low:
                direction, idx = -1, i; break
        if idx is None: continue
        entry = float(frame.iloc[idx].close * (1 + direction * slip))
        stop = float(high - 0.5 * rng) if direction == 1 else float(low + 0.5 * rng)
        risk = abs(entry - stop)
        if risk <= 0: continue
        target = entry + direction * reward_risk * risk
        qty = equity * risk_fraction / risk
        exit_price, outcome = float(frame.iloc[-1].close), "time"
        for _, c in frame.iloc[idx + 1:].iterrows():
            if (direction == 1 and c.low <= stop) or (direction == -1 and c.high >= stop):
                exit_price, outcome = stop, "stop"; break
            if (direction == 1 and c.high >= target) or (direction == -1 and c.low <= target):
                exit_price, outcome = target, "target"; break
        exit_price *= 1 - direction * slip
        pnl = direction * qty * (exit_price - entry); equity += pnl
        trades.append({"date": str(day), "direction": direction, "entry": entry, "exit": exit_price,
                       "pnl": pnl, "outcome": outcome, "equity": equity})
    result = pd.DataFrame(trades)
    if result.empty: return result, {"trades": 0, "ending_equity": starting_capital, "total_return": 0.0}
    peak = result.equity.cummax().clip(lower=starting_capital)
    metrics = {"trades": len(result), "ending_equity": float(equity), "total_return": float(equity / starting_capital - 1),
               "win_rate": float((result.pnl > 0).mean()),
               "profit_factor": float(result.loc[result.pnl > 0, "pnl"].sum() / -result.loc[result.pnl < 0, "pnl"].sum()) if (result.pnl < 0).any() else np.inf,
               "max_drawdown": float((result.equity / peak - 1).min()), "average_trade": float(result.pnl.mean())}
    return result, metrics


def run_daily_breakout(
    bars: pd.DataFrame,
    starting_capital: float = 2_200.0,
    risk_fraction: float = 0.005,
    reward_risk: float = 1.5,
    slippage_bps: float = 2.0,
) -> tuple[pd.DataFrame, dict]:
    """Daily prior-high/low breakout with next-session management."""
    bars = bars.sort_index().copy()
    if bars.index.tz is None:
        raise ValueError("bars must have a timezone-aware index")
    local = bars.tz_convert("America/New_York")
    if not (local.index.hour == 0).all():
        local = local.between_time("09:30", "16:00")
    daily = local.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    equity, trades, slip = starting_capital, [], slippage_bps / 10_000
    for i in range(1, len(daily) - 1):
        prev, cur = daily.iloc[i - 1], daily.iloc[i]
        direction = 1 if cur.close > prev.high else -1 if cur.close < prev.low else None
        if direction is None:
            continue
        entry = float(cur.close * (1 + direction * slip))
        stop = float(prev.high - 0.5 * (prev.high - prev.low)) if direction == 1 else float(prev.low + 0.5 * (prev.high - prev.low))
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        target = entry + direction * reward_risk * risk
        qty = equity * risk_fraction / risk
        exit_price, outcome = float(daily.iloc[i + 1].close), "time"
        for _, bar in daily.iloc[i + 1 :].iterrows():
            if (direction == 1 and bar.low <= stop) or (direction == -1 and bar.high >= stop):
                exit_price, outcome = stop, "stop"; break
            if (direction == 1 and bar.high >= target) or (direction == -1 and bar.low <= target):
                exit_price, outcome = target, "target"; break
        exit_price *= 1 - direction * slip
        pnl = direction * qty * (exit_price - entry); equity += pnl
        trades.append({"date": str(daily.index[i].date()), "direction": direction, "entry": entry,
                       "exit": exit_price, "pnl": pnl, "outcome": outcome, "equity": equity})
    result = pd.DataFrame(trades)
    if result.empty:
        return result, {"trades": 0, "ending_equity": starting_capital, "total_return": 0.0}
    peak = result.equity.cummax().clip(lower=starting_capital)
    metrics = {"trades": len(result), "ending_equity": float(equity), "total_return": float(equity / starting_capital - 1),
               "win_rate": float((result.pnl > 0).mean()),
               "profit_factor": float(result.loc[result.pnl > 0, "pnl"].sum() / -result.loc[result.pnl < 0, "pnl"].sum()) if (result.pnl < 0).any() else np.inf,
               "max_drawdown": float((result.equity / peak - 1).min()), "average_trade": float(result.pnl.mean())}
    return result, metrics


def run_opening_range_breakout(
    bars: pd.DataFrame,
    starting_capital: float = 2_200.0,
    risk_fraction: float = 0.005,
    reward_risk: float = 1.5,
    slippage_bps: float = 2.0,
) -> tuple[pd.DataFrame, dict]:
    """30-minute opening-range breakout, one trade per session."""
    bars = bars.sort_index().copy()
    if bars.index.tz is None:
        raise ValueError("bars must have a timezone-aware index")
    local = bars.tz_convert("America/New_York").between_time("09:30", "16:00")
    equity, trades, slip = starting_capital, [], slippage_bps / 10_000
    for day, frame in local.groupby(local.index.date):
        opening = frame.between_time("09:30", "10:00", inclusive="left")
        if len(opening) < 2:
            continue
        high, low = float(opening.high.max()), float(opening.low.min())
        rng = high - low
        if rng <= 0:
            continue
        direction = None; idx = None
        for i in range(len(opening), len(frame)):
            c = frame.iloc[i]
            if c.close > high:
                direction, idx = 1, i; break
            if c.close < low:
                direction, idx = -1, i; break
        if idx is None:
            continue
        entry = float(frame.iloc[idx].close * (1 + direction * slip))
        stop = low if direction == 1 else high
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        target = entry + direction * reward_risk * risk
        qty = equity * risk_fraction / risk
        exit_price, outcome = float(frame.iloc[-1].close), "time"
        for _, bar in frame.iloc[idx + 1:].iterrows():
            if (direction == 1 and bar.low <= stop) or (direction == -1 and bar.high >= stop):
                exit_price, outcome = stop, "stop"; break
            if (direction == 1 and bar.high >= target) or (direction == -1 and bar.low <= target):
                exit_price, outcome = target, "target"; break
        exit_price *= 1 - direction * slip
        pnl = direction * qty * (exit_price - entry); equity += pnl
        trades.append({"date": str(day), "direction": direction, "entry": entry, "exit": exit_price,
                       "pnl": pnl, "outcome": outcome, "equity": equity})
    result = pd.DataFrame(trades)
    if result.empty:
        return result, {"trades": 0, "ending_equity": starting_capital, "total_return": 0.0}
    peak = result.equity.cummax().clip(lower=starting_capital)
    metrics = {"trades": len(result), "ending_equity": float(equity), "total_return": float(equity / starting_capital - 1),
               "win_rate": float((result.pnl > 0).mean()),
               "profit_factor": float(result.loc[result.pnl > 0, "pnl"].sum() / -result.loc[result.pnl < 0, "pnl"].sum()) if (result.pnl < 0).any() else np.inf,
               "max_drawdown": float((result.equity / peak - 1).min()), "average_trade": float(result.pnl.mean())}
    return result, metrics


def run_institutional_opening_proxy(
    bars: pd.DataFrame,
    starting_capital: float = 2_200.0,
    risk_fraction: float = 0.005,
    reward_risk: float = 1.5,
    slippage_bps: float = 2.0,
) -> tuple[pd.DataFrame, dict]:
    """Opening-range breakout filtered by relative volume and VWAP alignment."""
    bars = bars.sort_index().copy()
    if bars.index.tz is None:
        raise ValueError("bars must have a timezone-aware index")
    local = bars.tz_convert("America/New_York").between_time("09:30", "16:00")
    local["vwap"] = (local.close * local.volume).groupby(local.index.date).cumsum() / local.volume.groupby(local.index.date).cumsum()
    local["rvol"] = local.volume / local.volume.groupby(local.index.date).transform(lambda s: s.shift(1).rolling(20, min_periods=5).mean())
    equity, trades, slip = starting_capital, [], slippage_bps / 10_000
    for day, frame in local.groupby(local.index.date):
        opening = frame.between_time("09:30", "10:00", inclusive="left")
        if len(opening) < 2:
            continue
        high, low = float(opening.high.max()), float(opening.low.min())
        for i in range(len(opening), len(frame)):
            c = frame.iloc[i]
            direction = 1 if c.close > high and c.close > c.vwap and c.rvol >= 1.2 else -1 if c.close < low and c.close < c.vwap and c.rvol >= 1.2 else None
            if direction is None:
                continue
            entry = float(c.close * (1 + direction * slip))
            stop = low if direction == 1 else high
            risk = abs(entry - stop)
            if risk <= 0:
                break
            target = entry + direction * reward_risk * risk
            qty = equity * risk_fraction / risk
            exit_price, outcome = float(frame.iloc[-1].close), "time"
            for _, bar in frame.iloc[i + 1:].iterrows():
                if (direction == 1 and bar.low <= stop) or (direction == -1 and bar.high >= stop):
                    exit_price, outcome = stop, "stop"; break
                if (direction == 1 and bar.high >= target) or (direction == -1 and bar.low <= target):
                    exit_price, outcome = target, "target"; break
            exit_price *= 1 - direction * slip
            pnl = direction * qty * (exit_price - entry); equity += pnl
            trades.append({"date": str(day), "direction": direction, "entry": entry, "exit": exit_price,
                           "pnl": pnl, "outcome": outcome, "equity": equity})
            break
    result = pd.DataFrame(trades)
    if result.empty:
        return result, {"trades": 0, "ending_equity": starting_capital, "total_return": 0.0}
    peak = result.equity.cummax().clip(lower=starting_capital)
    metrics = {"trades": len(result), "ending_equity": float(equity), "total_return": float(equity / starting_capital - 1),
               "win_rate": float((result.pnl > 0).mean()),
               "profit_factor": float(result.loc[result.pnl > 0, "pnl"].sum() / -result.loc[result.pnl < 0, "pnl"].sum()) if (result.pnl < 0).any() else np.inf,
               "max_drawdown": float((result.equity / peak - 1).min()), "average_trade": float(result.pnl.mean())}
    return result, metrics


def run_prior_day_breakout_vwap(
    bars: pd.DataFrame,
    starting_capital: float = 2_200.0,
    risk_fraction: float = 0.005,
    reward_risk: float = 1.5,
    slippage_bps: float = 2.0,
) -> tuple[pd.DataFrame, dict]:
    """Prior-day breakout requiring alignment with session VWAP."""
    bars = bars.sort_index().copy()
    if bars.index.tz is None:
        raise ValueError("bars must have a timezone-aware index")
    local = bars.tz_convert("America/New_York").between_time("09:30", "16:00").copy()
    local["vwap"] = (local.close * local.volume).groupby(local.index.date).cumsum() / local.volume.groupby(local.index.date).cumsum()
    daily = local.groupby(local.index.date).agg({"high": "max", "low": "min"})
    equity, trades, slip = starting_capital, [], slippage_bps / 10_000
    for day, frame in local.groupby(local.index.date):
        prior = daily.loc[daily.index < day]
        if prior.empty or len(frame) < 2:
            continue
        prev = prior.iloc[-1]; high, low = float(prev.high), float(prev.low)
        rng = high - low
        direction = None; idx = None
        for i in range(1, len(frame)):
            c, p = frame.iloc[i], frame.iloc[i - 1]
            if c.close > high and p.close <= high and c.close > c.vwap:
                direction, idx = 1, i; break
            if c.close < low and p.close >= low and c.close < c.vwap:
                direction, idx = -1, i; break
        if idx is None or rng <= 0:
            continue
        entry = float(frame.iloc[idx].close * (1 + direction * slip))
        stop = float(high - 0.5 * rng) if direction == 1 else float(low + 0.5 * rng)
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        target = entry + direction * reward_risk * risk
        qty = equity * risk_fraction / risk
        exit_price, outcome = float(frame.iloc[-1].close), "time"
        for _, c in frame.iloc[idx + 1:].iterrows():
            if (direction == 1 and c.low <= stop) or (direction == -1 and c.high >= stop):
                exit_price, outcome = stop, "stop"; break
            if (direction == 1 and c.high >= target) or (direction == -1 and c.low <= target):
                exit_price, outcome = target, "target"; break
        exit_price *= 1 - direction * slip
        pnl = direction * qty * (exit_price - entry); equity += pnl
        trades.append({"date": str(day), "direction": direction, "entry": entry, "exit": exit_price,
                       "pnl": pnl, "outcome": outcome, "equity": equity})
    result = pd.DataFrame(trades)
    if result.empty:
        return result, {"trades": 0, "ending_equity": starting_capital, "total_return": 0.0}
    peak = result.equity.cummax().clip(lower=starting_capital)
    metrics = {"trades": len(result), "ending_equity": float(equity), "total_return": float(equity / starting_capital - 1),
               "win_rate": float((result.pnl > 0).mean()),
               "profit_factor": float(result.loc[result.pnl > 0, "pnl"].sum() / -result.loc[result.pnl < 0, "pnl"].sum()) if (result.pnl < 0).any() else np.inf,
               "max_drawdown": float((result.equity / peak - 1).min()), "average_trade": float(result.pnl.mean())}
    return result, metrics
