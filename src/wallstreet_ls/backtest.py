from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import json

import numpy as np
import pandas as pd

from .config import StrategyConfig
from .strategy import calculate_targets
from .sentiment import sentiment_scores_as_of


def run_backtest(
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    config: StrategyConfig,
    start: str = "2019-01-01",
    benchmark: str = "SPY",
    transaction_cost_bps: float = 10.0,
    short_borrow_bps_annual: float = 0.0,
    stop_loss_pct: float | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Monthly, close-to-close backtest with turnover costs.

    A signal formed at a month-end close is applied from the next trading day.
    This avoids look-ahead. An optional annualized borrow-cost assumption is
    charged against gross short exposure. Dividends owed on shorts and locate
    failures are not modeled; adjusted prices capture corporate actions for
    price returns.
    """
    closes = closes.sort_index().ffill(limit=3)
    volumes = volumes.reindex_like(closes).fillna(0)
    strategy_symbols = [s for s in config.symbols if s in closes.columns]
    if benchmark not in closes.columns:
        raise ValueError(f"Benchmark {benchmark} is missing")

    returns = closes.pct_change(fill_method=None)
    eligible_dates = closes.index[closes.index >= pd.Timestamp(start, tz=closes.index.tz)]
    if eligible_dates.empty:
        raise ValueError("No observations in requested backtest period")
    rebalance_dates = (
        pd.Series(eligible_dates, index=eligible_dates)
        .groupby([eligible_dates.year, eligible_dates.month])
        .last()
        .tolist()
    )

    strategy_returns = pd.Series(0.0, index=eligible_dates)
    current = pd.Series(0.0, index=strategy_symbols)
    turnover_rows: list[dict] = []
    for index, rebalance_date in enumerate(rebalance_dates):
        # A month-end signal needs at least one subsequent close to produce a
        # measurable close-to-close return.  Do not include an uninvestable
        # final rebalance in turnover statistics.
        if index == len(rebalance_dates) - 1:
            break
        history_close = closes.loc[:rebalance_date, strategy_symbols]
        history_volume = volumes.loc[:rebalance_date, strategy_symbols]
        try:
            targets = calculate_targets(history_close, history_volume, config)
        except ValueError:
            continue
        new = targets.target_weight.reindex(strategy_symbols).fillna(0.0)
        turnover = float((new - current).abs().sum())
        cost = turnover * transaction_cost_bps / 10_000
        next_date = rebalance_dates[index + 1]
        period = eligible_dates[(eligible_dates > rebalance_date) & (eligible_dates <= next_date)]
        if len(period):
            period_returns = returns.loc[period, strategy_symbols].fillna(0).dot(new)
            short_exposure = float(new[new < 0].abs().sum())
            period_returns -= short_exposure * short_borrow_bps_annual / 10_000 / 252
            if stop_loss_pct is not None:
                cumulative = (1 + period_returns).cumprod() - 1
                hit = cumulative <= -abs(stop_loss_pct)
                if hit.any():
                    first = hit[hit].index[0]
                    period_returns.loc[first:] = 0.0
            strategy_returns.loc[period] = period_returns
            strategy_returns.loc[period[0]] -= cost
        turnover_rows.append({"date": str(rebalance_date.date()), "turnover": turnover, "cost_rate": cost})
        current = new

    benchmark_returns = returns.loc[eligible_dates, benchmark].fillna(0)
    # Normalize both series to the requested capital on the first measured day.
    strategy_returns.iloc[0] = 0.0
    benchmark_returns.iloc[0] = 0.0
    strategy_equity = config.starting_capital * (1 + strategy_returns).cumprod()
    benchmark_equity = config.starting_capital * (1 + benchmark_returns).cumprod()
    curve = pd.DataFrame({
        "strategy_equity": strategy_equity,
        "benchmark_equity": benchmark_equity,
        "strategy_return": strategy_returns,
        "benchmark_return": benchmark_returns,
    })
    metrics = {
        "strategy": _metrics(strategy_returns, strategy_equity),
        "benchmark": _metrics(benchmark_returns, benchmark_equity),
        "comparison": _comparison_metrics(strategy_returns, benchmark_returns),
        "assumptions": {
            "start": str(eligible_dates[0].date()),
            "end": str(eligible_dates[-1].date()),
            "initial_capital": config.starting_capital,
            "benchmark": benchmark,
            "rebalance": "monthly",
            "transaction_cost_bps": transaction_cost_bps,
            "short_borrow_bps_annual": short_borrow_bps_annual,
            "stop_loss_pct": stop_loss_pct,
            "gross_exposure_cap": config.max_gross_exposure,
            "universe": list(strategy_symbols),
            "limitations": [
                "Static present-day universe introduces survivorship bias.",
                "Dividends owed on shorts and locate failures are excluded.",
                "Daily bars do not model intraday fills or market impact.",
            ],
        },
        "average_monthly_turnover": float(np.mean([r["turnover"] for r in turnover_rows])) if turnover_rows else 0.0,
        "config": asdict(config),
    }
    return curve, metrics


def run_enhanced_backtest(
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    config: StrategyConfig,
    start: str = "2019-01-01",
    benchmark: str = "SPY",
    core_weight: float = 0.90,
    sleeve_count: int = 5,
    transaction_cost_bps: float = 10.0,
    sentiment_events: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Benchmark-aware long-only strategy: SPY core plus a factor sleeve."""
    if not 0 <= core_weight <= 1:
        raise ValueError("core_weight must be between zero and one")
    closes = closes.sort_index().ffill(limit=3)
    volumes = volumes.reindex_like(closes).fillna(0)
    symbols = [s for s in config.symbols if s in closes.columns]
    if benchmark not in closes.columns:
        raise ValueError(f"Benchmark {benchmark} is missing")

    returns = closes.pct_change(fill_method=None)
    eligible_dates = closes.index[closes.index >= pd.Timestamp(start, tz=closes.index.tz)]
    if eligible_dates.empty:
        raise ValueError("No observations in requested backtest period")
    rebalance_dates = (
        pd.Series(eligible_dates, index=eligible_dates)
        .groupby([eligible_dates.year, eligible_dates.month])
        .last()
        .tolist()
    )

    strategy_returns = pd.Series(0.0, index=eligible_dates)
    current = pd.Series(0.0, index=[benchmark, *symbols])
    turnover: list[float] = []
    sleeve_weight = 1.0 - core_weight
    for index, rebalance_date in enumerate(rebalance_dates):
        # See run_backtest: the final month-end has no forward return period.
        if index == len(rebalance_dates) - 1:
            break
        history = closes.loc[:rebalance_date, symbols]
        history_volume = volumes.loc[:rebalance_date, symbols]
        required = max(config.momentum_lookback + 1, config.volatility_lookback + 1)
        if len(history) < required:
            continue
        momentum = history.iloc[-config.momentum_skip - 1] / history.iloc[-config.momentum_lookback - 1] - 1
        volatility = history.pct_change(fill_method=None).tail(config.volatility_lookback).std()
        dollar_volume = (history.tail(20) * history_volume.tail(20)).mean()
        eligible = (
            (history.iloc[-1] >= config.min_price)
            & (dollar_volume >= config.min_average_dollar_volume)
            & momentum.notna()
            & volatility.gt(0)
        )
        candidates = momentum.index[eligible]
        if len(candidates) < sleeve_count:
            continue
        score = 0.55 * momentum[candidates].rank(pct=True) + 0.25 * (-volatility[candidates]).rank(pct=True)
        if sentiment_events is not None:
            sentiment = sentiment_scores_as_of(sentiment_events, rebalance_date, list(candidates))
            score += 0.15 * sentiment["news"].rank(pct=True) + 0.05 * sentiment["social"].rank(pct=True)
            score = score[sentiment["news"] > -0.60]
        else:
            score += 0.10
        selected = score.nlargest(sleeve_count).index
        new = pd.Series(0.0, index=current.index)
        new.loc[benchmark] = core_weight
        new.loc[selected] = sleeve_weight / sleeve_count
        traded = float((new - current).abs().sum())
        cost = traded * transaction_cost_bps / 10_000
        next_date = rebalance_dates[index + 1]
        period = eligible_dates[(eligible_dates > rebalance_date) & (eligible_dates <= next_date)]
        if len(period):
            strategy_returns.loc[period] = returns.loc[period, new.index].fillna(0).dot(new)
            strategy_returns.loc[period[0]] -= cost
        turnover.append(traded)
        current = new

    benchmark_returns = returns.loc[eligible_dates, benchmark].fillna(0)
    strategy_returns.iloc[0] = 0.0
    benchmark_returns.iloc[0] = 0.0
    strategy_equity = config.starting_capital * (1 + strategy_returns).cumprod()
    benchmark_equity = config.starting_capital * (1 + benchmark_returns).cumprod()
    curve = pd.DataFrame({
        "strategy_equity": strategy_equity,
        "benchmark_equity": benchmark_equity,
        "strategy_return": strategy_returns,
        "benchmark_return": benchmark_returns,
    })
    metrics = {
        "strategy": _metrics(strategy_returns, strategy_equity),
        "benchmark": _metrics(benchmark_returns, benchmark_equity),
        "comparison": _comparison_metrics(strategy_returns, benchmark_returns),
        "assumptions": {
            "start": str(eligible_dates[0].date()),
            "end": str(eligible_dates[-1].date()),
            "initial_capital": config.starting_capital,
            "benchmark": benchmark,
            "core_weight": core_weight,
            "factor_sleeve_weight": sleeve_weight,
            "factor_sleeve_count": sleeve_count,
            "factor_score": "55% momentum, 25% low volatility, 15% news, 5% social",
            "sentiment_enabled": sentiment_events is not None,
            "rebalance": "monthly",
            "transaction_cost_bps": transaction_cost_bps,
            "limitations": [
                "Static present-day stock universe introduces survivorship bias.",
                "Quality and valuation are excluded without point-in-time fundamentals.",
                "Daily bars do not model taxes, intraday fills, or market impact.",
            ],
        },
        "average_monthly_turnover": float(np.mean(turnover)) if turnover else 0.0,
    }
    return curve, metrics


def run_video_investing_proxy(
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    config: StrategyConfig,
    start: str = "2019-01-01",
    benchmark: str = "SPY",
    transaction_cost_bps: float = 10.0,
) -> tuple[pd.DataFrame, dict]:
    """Backtestable proxy for the video's long-term, diversified approach.

    The video does not define an algorithm. This conservative proxy keeps 80%
    in SPY and allocates 20% monthly to ten liquid large-cap names ranked by
    trailing momentum and lower volatility. It excludes sentiment and
    fundamentals because those inputs are not point-in-time in this dataset.
    """
    curve, metrics = run_enhanced_backtest(
        closes, volumes, config, start=start, benchmark=benchmark,
        core_weight=0.80, sleeve_count=10,
        transaction_cost_bps=transaction_cost_bps,
        sentiment_events=None,
    )
    metrics["assumptions"]["strategy_label"] = "Video investing proxy (80% SPY / 20% factor sleeve)"
    metrics["assumptions"]["video_proxy_assumptions"] = [
        "Long-term diversified core represented by SPY.",
        "Active sleeve uses momentum and lower volatility as objective proxies for stock selection.",
        "No leverage, shorting, contributions, taxes, or point-in-time fundamental data.",
    ]
    return curve, metrics


def run_momentum_video_proxy(
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    config: StrategyConfig,
    start: str = "2019-01-01",
    benchmark: str = "SPY",
    transaction_cost_bps: float = 10.0,
    stock_count: int = 10,
) -> tuple[pd.DataFrame, dict]:
    """12-1 month long-only momentum proxy from the Algovibes video."""
    curve, metrics = run_enhanced_backtest(
        closes, volumes, config, start=start, benchmark=benchmark,
        core_weight=0.0, sleeve_count=stock_count,
        transaction_cost_bps=transaction_cost_bps, sentiment_events=None,
    )
    metrics["assumptions"]["strategy_label"] = "12-1 month long-only momentum proxy"
    metrics["assumptions"]["video_source"] = "Algovibes momentum strategy (YouTube 5W_Lpz1ZuTI)"
    metrics["assumptions"]["video_proxy_assumptions"] = [
        "Monthly selection from the static large-cap universe.",
        "Momentum uses a 12-month lookback excluding the latest month.",
        "The implementation adds a low-volatility tie-break score because the video does not specify portfolio weighting.",
    ]
    return curve, metrics


def save_results(curve: pd.DataFrame, metrics: dict, output_dir: str | Path) -> None:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    curve.to_csv(output / "backtest_curve.csv", index_label="date")
    (output / "backtest_metrics.json").write_text(json.dumps(metrics, indent=2))


def _metrics(returns: pd.Series, equity: pd.Series) -> dict:
    years = max(len(returns) / 252, 1 / 252)
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)
    annual_vol = float(returns.std() * np.sqrt(252))
    sharpe = float(returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0.0
    drawdown = equity / equity.cummax() - 1
    return {
        "ending_equity": float(equity.iloc[-1]),
        "total_return": total_return,
        "cagr": cagr,
        "annual_volatility": annual_vol,
        "sharpe_zero_rf": sharpe,
        "max_drawdown": float(drawdown.min()),
        "positive_months": float((returns.add(1).groupby([returns.index.year, returns.index.month]).prod() - 1 > 0).mean()),
    }


def _comparison_metrics(strategy_returns: pd.Series, benchmark_returns: pd.Series) -> dict:
    """Risk and relative-performance statistics for matched daily returns."""
    aligned = pd.concat(
        [strategy_returns.rename("strategy"), benchmark_returns.rename("benchmark")],
        axis=1,
    ).dropna()
    strategy = aligned["strategy"]
    benchmark = aligned["benchmark"]
    active = strategy - benchmark
    benchmark_variance = benchmark.var()
    beta = float(strategy.cov(benchmark) / benchmark_variance) if benchmark_variance > 0 else 0.0
    tracking_error = float(active.std() * np.sqrt(252))
    downside_deviation = float(strategy[strategy < 0].std() * np.sqrt(252))
    return {
        "correlation": float(strategy.corr(benchmark)),
        "beta": beta,
        "annualized_alpha_zero_rf": float((strategy.mean() - beta * benchmark.mean()) * 252),
        "tracking_error": tracking_error,
        "information_ratio": float(active.mean() / active.std() * np.sqrt(252)) if active.std() > 0 else 0.0,
        "sortino_zero_rf": float(strategy.mean() * 252 / downside_deviation) if downside_deviation > 0 else 0.0,
    }
