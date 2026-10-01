"""Publish a read-only Alpaca account snapshot for the local dashboard."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from .alpaca_adapter import clients


def build_snapshot() -> dict:
    """Return only display-safe account and position fields; never expose credentials."""
    _, trading_client, paper = clients()
    account = trading_client.get_account()
    equity = float(account.equity)
    positions = []
    for position in trading_client.get_all_positions():
        market_value = float(position.market_value)
        positions.append({
            "symbol": position.symbol,
            "side": str(position.side).split(".")[-1].upper(),
            "quantity": float(position.qty),
            "market_value": market_value,
            "weight": market_value / equity if equity else 0.0,
            "price": float(position.current_price),
            "day_change": float(position.change_today),
            "unrealized_pl": float(position.unrealized_pl),
            "unrealized_plpc": float(position.unrealized_plpc),
        })
    positions.sort(key=lambda row: abs(row["market_value"]), reverse=True)
    gross_exposure = sum(abs(row["market_value"]) for row in positions) / equity if equity else 0.0
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "account_mode": "PAPER" if paper else "LIVE",
        "equity": equity,
        "cash": float(account.cash),
        "last_equity": float(account.last_equity),
        "day_pl": equity - float(account.last_equity),
        "gross_exposure": gross_exposure,
        "positions": positions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish a read-only Alpaca snapshot for the dashboard")
    parser.add_argument("--output", default="dashboard/public/alpaca_snapshot.json")
    parser.add_argument("--live", action="store_true", help="read the live Alpaca account instead of the configured paper account")
    parser.add_argument("--watch", action="store_true", help="refresh the read-only snapshot every minute")
    args = parser.parse_args()
    load_dotenv(".env.local")
    load_dotenv()
    if args.live:
        os.environ["ALPACA_PAPER"] = "false"
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            snapshot = build_snapshot()
            temporary = output.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(snapshot, indent=2))
            temporary.replace(output)
            print(f"Saved dashboard snapshot at {snapshot['as_of']}", flush=True)
        except Exception as error:
            if not args.watch:
                raise
            print(f"Account sync failed ({type(error).__name__}); retaining last snapshot and retrying in 60 seconds", flush=True)
        if not args.watch:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
