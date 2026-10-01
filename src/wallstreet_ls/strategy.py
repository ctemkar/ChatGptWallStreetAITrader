from __future__ import annotations

import numpy as np
import pandas as pd

from .config import StrategyConfig


def calculate_targets(
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    config: StrategyConfig,
    exclude_from_shorts: set[str] | None = None,
) -> pd.DataFrame:
    """Return target weights from adjusted daily close and volume matrices.

    Signals use 12-1 month momentum. Each side is inverse-volatility weighted,
    capped per name, and then scaled toward the portfolio volatility target.
    """
    required = config.momentum_lookback + 1
    if len(closes) < required:
        raise ValueError(f"Need at least {required} daily observations")
    if not closes.columns.equals(volumes.columns):
        volumes = volumes.reindex(columns=closes.columns)

    latest = closes.iloc[-1]
    adv = (closes * volumes).tail(20).mean()
    eligible = (
        (latest >= config.min_price)
        & (adv >= config.min_average_dollar_volume)
        & closes.notna().tail(required).all()
    )
    symbols = eligible[eligible].index
    if len(symbols) < config.long_count + config.short_count:
        raise ValueError("Not enough liquid symbols for both long and short books")

    selected = closes[symbols]
    signal = selected.iloc[-1 - config.momentum_skip] / selected.iloc[-1 - config.momentum_lookback] - 1
    daily_returns = selected.pct_change(fill_method=None)
    annual_vol = daily_returns.tail(config.volatility_lookback).std() * np.sqrt(252)
    score = signal.rank(pct=True) - 0.5

    longs = score.nlargest(config.long_count).index
    side_budget = min(config.gross_exposure, config.max_gross_exposure) / 2
    # Whole-share short sleeve: skip names where one share would breach the
    # position cap on starting_capital, and never overlap the long sleeve.
    # Walk worst momentum ranks until short_count fills. Names with |raw qty|
    # under 1 share but still under the cap are opened as -1 in build_trade_plan.
    max_short_price = config.starting_capital * config.max_position_weight
    long_set = set(longs)
    excluded = set(exclude_from_shorts or ())
    short_candidates = []
    for symbol in score.nsmallest(len(score)).index:
        if symbol in long_set or symbol in excluded:
            continue
        price = float(latest[symbol])
        if price > max_short_price:
            continue  # 1 share would exceed max_position_weight
        short_candidates.append(symbol)
        if len(short_candidates) >= config.short_count:
            break
    if len(short_candidates) < config.short_count:
        raise ValueError(
            f"Only {len(short_candidates)} whole-share-affordable short names; "
            f"need {config.short_count}"
        )
    shorts = pd.Index(short_candidates)

    weights = pd.Series(0.0, index=selected.columns)
    weights.loc[longs] = _side_weights(annual_vol.loc[longs], side_budget, config.max_position_weight)
    weights.loc[shorts] = -_side_weights(annual_vol.loc[shorts], side_budget, config.max_position_weight)

    portfolio_returns = daily_returns[weights.index].fillna(0).dot(weights)
    realized_vol = portfolio_returns.tail(config.volatility_lookback).std() * np.sqrt(252)
    if np.isfinite(realized_vol) and realized_vol > 0:
        scaler = min(1.0, config.target_annual_volatility / realized_vol)
        weights *= scaler

    result = pd.DataFrame({
        "signal": signal,
        "annual_volatility": annual_vol,
        "target_weight": weights,
        "last_price": latest.reindex(weights.index),
        "average_dollar_volume": adv.reindex(weights.index),
    })
    return result[result.target_weight != 0].sort_values("target_weight", ascending=False)


def calculate_momentum_targets(
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    config: StrategyConfig,
    stock_count: int = 10,
) -> pd.DataFrame:
    """Long-only 12-1 momentum targets for the YouTube strategy proxy."""
    required = config.momentum_lookback + 1
    if len(closes) < required:
        raise ValueError(f"Need at least {required} daily observations")
    volumes = volumes.reindex_like(closes).fillna(0)
    latest, adv = closes.iloc[-1], (closes * volumes).tail(20).mean()
    eligible = (latest >= config.min_price) & (adv >= config.min_average_dollar_volume) & closes.notna().tail(required).all()
    symbols = eligible[eligible].index
    if len(symbols) < stock_count:
        raise ValueError("Not enough liquid symbols for momentum sleeve")
    signal = closes[symbols].iloc[-1 - config.momentum_skip] / closes[symbols].iloc[-1 - config.momentum_lookback] - 1
    selected = signal.nlargest(stock_count).index
    weight = 1.0 / stock_count
    return pd.DataFrame({
        "signal": signal.loc[selected],
        "annual_volatility": closes[selected].pct_change(fill_method=None).tail(config.volatility_lookback).std() * np.sqrt(252),
        "target_weight": weight,
        "last_price": latest.loc[selected],
        "average_dollar_volume": adv.loc[selected],
    })


def calculate_diversified_momentum_targets(
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    config: StrategyConfig,
    stock_count: int = 20,
) -> pd.DataFrame:
    """Long-only momentum targets with a low-volatility tie-breaker.

    This is the executable counterpart to the validated research proxy. It
    ranks a liquid universe using 55% 12-1-month momentum and 25% inverse
    20-day volatility, then holds an equal-weighted diversified basket.
    """
    required = max(config.momentum_lookback + 1, config.volatility_lookback + 1)
    if len(closes) < required:
        raise ValueError(f"Need at least {required} daily observations")
    volumes = volumes.reindex_like(closes).fillna(0)
    latest = closes.iloc[-1]
    adv = (closes * volumes).tail(20).mean()
    eligible = (
        (latest >= config.min_price)
        & (adv >= config.min_average_dollar_volume)
        & closes.notna().tail(required).all()
    )
    symbols = eligible[eligible].index
    if len(symbols) < stock_count:
        raise ValueError("Not enough liquid symbols for diversified momentum sleeve")
    selected_close = closes[symbols]
    momentum = selected_close.iloc[-1 - config.momentum_skip] / selected_close.iloc[-1 - config.momentum_lookback] - 1
    volatility = selected_close.pct_change(fill_method=None).tail(config.volatility_lookback).std()
    candidates = momentum.index[momentum.notna() & volatility.gt(0)]
    if len(candidates) < stock_count:
        raise ValueError("Not enough valid symbols for diversified momentum sleeve")
    score = 0.55 * momentum[candidates].rank(pct=True) + 0.25 * (-volatility[candidates]).rank(pct=True)
    selected = score.nlargest(stock_count).index
    return pd.DataFrame({
        "signal": momentum.loc[selected],
        "annual_volatility": volatility.loc[selected] * np.sqrt(252),
        "target_weight": 1.0 / stock_count,
        "last_price": latest.loc[selected],
        "average_dollar_volume": adv.loc[selected],
    }).sort_values("signal", ascending=False)


def _side_weights(volatility: pd.Series, budget: float, cap: float) -> pd.Series:
    inverse = 1 / volatility.replace(0, np.nan)
    inverse = inverse.replace([np.inf, -np.inf], np.nan).fillna(0)
    if inverse.sum() == 0:
        inverse[:] = 1
    raw = inverse / inverse.sum() * budget
    capped = raw.clip(upper=cap)
    # Do not redistribute cap overflow; preserving the cap is the risk priority.
    return capped
