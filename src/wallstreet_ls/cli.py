from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

from .alpaca_adapter import build_trade_plan, clients, fetch_history, submit_plan
from .config import StrategyConfig
from .strategy import calculate_diversified_momentum_targets, calculate_targets, calculate_momentum_targets


def stock_count_for_strategy(strategy: str, requested: int | None) -> int:
    """Return the validated basket size unless the caller explicitly overrides it."""
    if requested is not None:
        return requested
    return 20 if strategy == "diversified-momentum" else 10


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper-first U.S. equity long/short strategy")
    parser.add_argument("--submit", action="store_true", help="submit the generated orders")
    parser.add_argument("--strategy", choices=("long-short", "momentum", "diversified-momentum"), default="long-short")
    parser.add_argument("--stocks", type=int, help="override the number of stocks in a long-only sleeve")
    parser.add_argument("--extended-hours", action="store_true", help="use DAY limit orders eligible for extended hours")
    parser.add_argument("--output", default="trade_plan.csv", help="trade-plan CSV path")
    args = parser.parse_args()
    load_dotenv(".env.local")
    load_dotenv()

    data_client, trading_client, paper = clients()
    account = trading_client.get_account()
    print(f"Account mode: {'PAPER' if paper else 'LIVE'}; account_id: {account.id}")
    config = StrategyConfig()
    closes, volumes = fetch_history(data_client, config)
    if args.strategy == "momentum":
        targets = calculate_momentum_targets(closes, volumes, config, stock_count_for_strategy(args.strategy, args.stocks))
    elif args.strategy == "diversified-momentum":
        # The validated replacement specification is a 20-stock basket. Keep
        # that default distinct from the legacy 10-stock momentum strategy.
        targets = calculate_diversified_momentum_targets(closes, volumes, config, stock_count_for_strategy(args.strategy, args.stocks))
    else:
        excluded_shorts: set[str] = set()
        targets = None
        plan = None
        for _ in range(config.short_count + 2):
            targets = calculate_targets(closes, volumes, config, exclude_from_shorts=excluded_shorts)
            plan = build_trade_plan(trading_client, targets, config)
            blocked = [
                sym for sym, row in plan.iterrows()
                if float(row.target_weight) < 0 and float(row.target_qty) == 0
            ]
            if not blocked:
                break
            excluded_shorts.update(blocked)
            print(f"Replacing blocked shorts: {', '.join(blocked)}")
        else:
            print("Warning: exhausted short replacements; some shorts may still be blocked")
    plan.to_csv(args.output)
    print(plan.to_string())
    print(f"\nSaved {args.output}; gross target={plan.target_weight.abs().sum():.2%}")

    if not args.submit:
        print("Dry run only. Add --submit to send orders.")
        return
    if not paper and os.getenv("ALLOW_LIVE_TRADING") != "I_UNDERSTAND_THE_RISK":
        raise RuntimeError("Live trading is locked. Use paper mode or explicitly unlock it.")
    ids = submit_plan(trading_client, plan, extended_hours=args.extended_hours)
    print(f"Submitted {len(ids)} orders: {', '.join(ids)}")


if __name__ == "__main__":
    main()
