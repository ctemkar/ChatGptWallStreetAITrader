from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from dataclasses import replace

from dotenv import load_dotenv

from .backtest import run_backtest, run_enhanced_backtest, run_video_investing_proxy, run_momentum_video_proxy, save_results
from .config import StrategyConfig
from .sentiment import load_sentiment_events


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the U.S. equity long/short strategy")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--output", default="backtest_results")
    parser.add_argument(
        "--dashboard-public",
        help="optional dashboard public directory to receive the result JSON and curve CSV",
    )
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--short-borrow-bps", type=float, default=0.0,
                        help="annualized borrow cost applied to gross short exposure")
    parser.add_argument("--mode", choices=("original", "enhanced", "video-proxy", "momentum-video", "etf-momentum", "short-momentum"), default="original")
    parser.add_argument("--core-weight", type=float, default=0.90)
    parser.add_argument("--stocks", type=int, default=10, help="number of stocks for momentum-video mode")
    parser.add_argument("--stop-loss", type=float, default=0.10, help="portfolio-period stop for short-momentum mode")
    parser.add_argument("--sentiment-events", help="Point-in-time news/social CSV")
    args = parser.parse_args()
    load_dotenv(".env.local")
    load_dotenv()

    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    config = StrategyConfig(min_average_dollar_volume=1.0)
    if args.mode == "etf-momentum":
        config = replace(config, symbols=("QQQ", "IWM", "MDY", "EFA", "EEM", "TLT", "GLD", "VNQ", "XLV", "XLE"))
    elif args.mode == "short-momentum":
        config = replace(config, long_count=0, short_count=10, gross_exposure=0.60, max_gross_exposure=0.60)
    symbols = list(config.symbols) + ["SPY"]
    client = StockHistoricalDataClient(key, secret)
    bars = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=datetime(2018, 6, 1, tzinfo=timezone.utc),
        end=datetime.now(timezone.utc),
        adjustment=Adjustment.ALL,
        feed=DataFeed.IEX,
    )).df.reset_index()
    closes = bars.pivot(index="timestamp", columns="symbol", values="close").sort_index()
    volumes = bars.pivot(index="timestamp", columns="symbol", values="volume").sort_index()
    if args.mode == "short-momentum":
        curve, metrics = run_backtest(closes, volumes, config, args.start, transaction_cost_bps=args.cost_bps,
                                      short_borrow_bps_annual=args.short_borrow_bps, stop_loss_pct=args.stop_loss)
        metrics["assumptions"]["strategy_label"] = "Bottom-10 momentum short basket"
        metrics["assumptions"]["short_proxy_assumptions"] = [
            "Short the ten lowest 12-1 month momentum names monthly.",
            "Gross exposure capped at 60%; borrow fees and dividends owed are not modeled.",
        ]
    elif args.mode == "etf-momentum":
        curve, metrics = run_momentum_video_proxy(
            closes, volumes, config, args.start,
            transaction_cost_bps=args.cost_bps, stock_count=min(args.stocks, len(config.symbols)),
        )
        metrics["assumptions"]["strategy_label"] = "ETF momentum rotation proxy"
    elif args.mode == "momentum-video":
        curve, metrics = run_momentum_video_proxy(
            closes, volumes, config, args.start,
            transaction_cost_bps=args.cost_bps, stock_count=args.stocks,
        )
    elif args.mode == "video-proxy":
        curve, metrics = run_video_investing_proxy(
            closes, volumes, config, args.start,
            transaction_cost_bps=args.cost_bps,
        )
    elif args.mode == "enhanced":
        sentiment_events = load_sentiment_events(args.sentiment_events) if args.sentiment_events else None
        curve, metrics = run_enhanced_backtest(
            closes, volumes, config, args.start,
            core_weight=args.core_weight,
            transaction_cost_bps=args.cost_bps,
            sentiment_events=sentiment_events,
        )
    else:
        curve, metrics = run_backtest(closes, volumes, config, args.start, transaction_cost_bps=args.cost_bps,
                                      short_borrow_bps_annual=args.short_borrow_bps)
    save_results(curve, metrics, args.output)
    if args.dashboard_public:
        dashboard_public = Path(args.dashboard_public)
        dashboard_public.mkdir(parents=True, exist_ok=True)
        for filename in ("backtest_metrics.json", "backtest_curve.csv"):
            shutil.copy2(Path(args.output) / filename, dashboard_public / filename)
    print(f"Saved results to {args.output}")
    print(metrics)


if __name__ == "__main__":
    main()
