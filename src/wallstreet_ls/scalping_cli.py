from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .scalping_backtest import run_quick_flip, run_sneaky_pivot, run_prior_day_breakout, run_daily_breakout, run_opening_range_breakout, run_institutional_opening_proxy, run_prior_day_breakout_vwap


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the Quick Flip opening-range scalper")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--output", default="quick_flip_results")
    parser.add_argument("--strategy", choices=("quick-flip", "sneaky-pivot", "prior-day-breakout", "daily-breakout", "opening-range", "institutional-opening", "prior-day-vwap"), default="quick-flip")
    parser.add_argument("--minutes", type=int, default=5, help="intraday candle interval")
    args = parser.parse_args()
    load_dotenv(".env.local")
    load_dotenv()
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    # Alpaca's minute timeframe supports 1–59 minutes; represent 60 minutes
    # as a one-hour timeframe.
    if args.strategy == "daily-breakout":
        timeframe = TimeFrame(1, TimeFrameUnit.Day)
    elif args.minutes >= 60 and args.minutes % 60 == 0:
        timeframe = TimeFrame(args.minutes // 60, TimeFrameUnit.Hour)
    else:
        timeframe = TimeFrame(15 if args.strategy == "sneaky-pivot" else args.minutes, TimeFrameUnit.Minute)
    bars = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=[args.symbol],
        timeframe=timeframe,
        start=datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc),
        end=datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc),
        adjustment=Adjustment.ALL,
        feed=DataFeed.IEX,
    )).df.xs(args.symbol).rename(columns=str.lower)
    runner = (run_sneaky_pivot if args.strategy == "sneaky-pivot" else
              run_prior_day_breakout if args.strategy == "prior-day-breakout" else
              run_daily_breakout if args.strategy == "daily-breakout" else
              run_opening_range_breakout if args.strategy == "opening-range" else
              run_institutional_opening_proxy if args.strategy == "institutional-opening" else run_quick_flip)
    if args.strategy == "prior-day-vwap":
        runner = run_prior_day_breakout_vwap
    trades, metrics = runner(bars)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    trades.to_csv(output / "trades.csv", index=False)
    (output / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(metrics)


if __name__ == "__main__":
    main()
