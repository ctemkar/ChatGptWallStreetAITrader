"""Run a selected portfolio strategy on a schedule; disabled by default for safety."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(description="Scheduled portfolio rebalance runner")
    p.add_argument("--every-hours", type=float, default=24.0)
    p.add_argument("--stocks", type=int, help="override the number of stocks in a long-only sleeve")
    p.add_argument("--strategy", choices=("long-short", "momentum", "diversified-momentum"), default="long-short")
    p.add_argument("--submit", action="store_true", help="submit orders; otherwise dry-run")
    p.add_argument("--once", action="store_true")
    p.add_argument("--output-dir", default="scheduled_plans")
    args = p.parse_args()
    if args.submit and os.getenv("AUTO_TRADER_ENABLE") != "I_UNDERSTAND_AUTOMATION":
        raise SystemExit("Refusing submission: set AUTO_TRADER_ENABLE=I_UNDERSTAND_AUTOMATION")
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = out / f"{args.strategy}_{stamp}.csv"
        cmd = [sys.executable, "-m", "wallstreet_ls.cli", "--strategy", args.strategy,
               "--output", str(path)]
        if args.strategy in ("momentum", "diversified-momentum"):
            default_stocks = 20 if args.strategy == "diversified-momentum" else 10
            cmd.extend(["--stocks", str(args.stocks or default_stocks)])
        if args.submit:
            cmd.append("--submit")
        print("Running:", " ".join(cmd), flush=True)
        subprocess.run(cmd, check=False)
        if args.once:
            return
        time.sleep(max(300.0, args.every_hours * 3600.0))


if __name__ == "__main__":
    main()
