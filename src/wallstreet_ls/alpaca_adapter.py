from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import uuid

import pandas as pd

from .config import StrategyConfig, is_rebalance_delta_actionable


def clients():
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient

    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    if paper:
        key = os.getenv("ALPACA_PAPER_API_KEY", os.getenv("ALPACA_API_KEY"))
        secret = os.getenv("ALPACA_PAPER_SECRET_KEY", os.getenv("ALPACA_SECRET_KEY"))
    else:
        key = os.getenv("ALPACA_API_KEY")
        secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Missing Alpaca credentials for the selected paper/live mode")
    return (
        StockHistoricalDataClient(key, secret),
        TradingClient(key, secret, paper=paper),
        paper,
    )


def fetch_history(data_client, config: StrategyConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    request = StockBarsRequest(
        symbol_or_symbols=list(config.symbols),
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        adjustment=Adjustment.ALL,
        feed=DataFeed.IEX,
    )
    frame = data_client.get_stock_bars(request).df.reset_index()
    closes = frame.pivot(index="timestamp", columns="symbol", values="close").sort_index()
    volumes = frame.pivot(index="timestamp", columns="symbol", values="volume").sort_index()
    common = closes.columns.intersection(volumes.columns)
    return closes[common], volumes[common]


def build_trade_plan(
    trading_client,
    targets: pd.DataFrame,
    config: StrategyConfig | None = None,
) -> pd.DataFrame:
    """Convert weights to whole-share deltas after Alpaca asset checks."""
    account = trading_client.get_account()
    config = config or StrategyConfig()
    account_equity = float(account.equity)
    equity = min(account_equity, config.starting_capital)
    if (targets.target_weight < 0).any() and account_equity < 2_000:
        raise RuntimeError(
            "Alpaca requires at least $2,000 account equity for short selling; "
            "the account is currently below that threshold."
        )
    positions = {p.symbol: p for p in trading_client.get_all_positions()}
    rows = []
    # Include stale strategy positions so a rebalance can actually flatten them.
    for symbol in sorted(set(targets.index) | set(positions)):
        is_target = symbol in targets.index
        row = targets.loc[symbol] if is_target else None
        asset = trading_client.get_asset(symbol)
        weight = float(row.target_weight) if is_target else 0.0
        allowed = asset.tradable and (weight >= 0 or (asset.shortable and asset.easy_to_borrow))
        position = positions.get(symbol)
        price = float(row.last_price) if is_target else float(position.current_price)
        raw_desired = equity * weight / price if allowed else 0
        # Alpaca supports fractional quantities for eligible equities, but not
        # for opening short sales.  Round short targets toward zero so a
        # rebalance can never submit an invalid fractional short order.
        # Whole-share–aware shorts: skip names where 1 share exceeds the
        # position cap; if |raw| < 1 but 1 share fits under the cap, open
        # exactly -1 rather than silently dropping the sleeve name.
        if weight < 0:
            one_share_weight = price / equity if equity else float("inf")
            max_w = config.max_position_weight
            if not allowed or one_share_weight > max_w:
                desired = 0
            else:
                desired = int(raw_desired)  # toward 0
                if desired == 0 and abs(raw_desired) > 0:
                    desired = -1
        elif allowed and getattr(asset, "fractionable", False):
            desired = round(raw_desired, 6)
        else:
            desired = int(raw_desired)
        current = float(position.qty) if position is not None else 0.0
        rows.append({
            "symbol": symbol,
            "target_weight": weight,
            "last_price": price,
            "current_qty": current,
            "target_qty": desired,
            "delta_qty": desired - current,
            "eligible": bool(allowed),
            "sizing_equity": equity,
        })
    return pd.DataFrame(rows).set_index("symbol")


def _order_legs(current: float, target: float) -> list[float]:
    """Split a position change into Alpaca-safe legs.

    Long→short or short→long cannot be one order: selling more than the long
    qty (or buying more than the short cover) fails with insufficient qty.
    Flatten first, then open the other side.
    """
    delta = target - current
    if delta == 0:
        return []
    if current > 0 and target < 0:
        return [-current, target]  # sell to flat, then sell to short
    if current < 0 and target > 0:
        return [-current, target]  # buy to cover, then buy to long
    return [delta]


def submit_plan(trading_client, plan: pd.DataFrame, extended_hours: bool = False) -> list[str]:
    from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
    from alpaca.trading.requests import GetOrderByIdRequest, LimitOrderRequest, MarketOrderRequest
    import time

    order_ids = []
    legs = []
    for symbol, row in plan.iterrows():
        current = float(row.current_qty)
        target = float(row.target_qty)
        price = float(row.last_price)
        crosses = (current > 0 and target < 0) or (current < 0 and target > 0)
        for i, delta in enumerate(_order_legs(current, target)):
            # $25 micro-rebalance floor (env MIN_REBALANCE_NOTIONAL_USD); membership/flip exempt.
            # Alpaca hard $1 floor still enforced inside is_rebalance_delta_actionable.
            if not is_rebalance_delta_actionable(current, target, delta, price):
                continue
            phase = 1 if crosses and i == 1 else 0
            reducing = (current * delta) < 0 or (crosses and i == 0)
            legs.append({
                "symbol": symbol,
                "delta_qty": delta,
                "last_price": price,
                "phase": phase,
                "reducing": reducing,
            })
    if not legs:
        return order_ids

    def _submit_rows(rows: pd.DataFrame) -> list[str]:
        ids = []
        for _, row in rows.iterrows():
            delta = float(row.delta_qty)
            kwargs = dict(
                symbol=row.symbol,
                qty=abs(delta),
                side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=f"wallstreet-ls-{datetime.now(timezone.utc):%Y%m%d}-{row.symbol}-{uuid.uuid4().hex[:8]}",
            )
            if extended_hours:
                order = LimitOrderRequest(**kwargs, limit_price=round(float(row.last_price), 2), extended_hours=True)
            else:
                order = MarketOrderRequest(**kwargs)
            result = trading_client.submit_order(order_data=order)
            ids.append(str(result.id))
        return ids

    def _wait_filled(ids: list[str], timeout_sec: float = 120.0) -> None:
        """Block until flatten legs fill so cross-zero opens see flat inventory."""
        if not ids:
            return
        deadline = time.time() + timeout_sec
        pending = set(ids)
        while pending and time.time() < deadline:
            done = set()
            for oid in pending:
                order = trading_client.get_order_by_id(oid)
                status = str(order.status)
                if status in {"OrderStatus.FILLED", "filled", "OrderStatus.PARTIALLY_FILLED", "partially_filled"}:
                    # require fully filled for flatten safety
                    filled = float(order.filled_qty or 0)
                    qty = float(order.qty or 0)
                    if status.endswith("FILLED") or status == "filled" or (qty and filled >= qty - 1e-9):
                        if "PARTIAL" not in status.upper() and status != "partially_filled":
                            done.add(oid)
                        elif qty and filled >= qty - 1e-9:
                            done.add(oid)
                elif status in {"OrderStatus.CANCELED", "canceled", "OrderStatus.EXPIRED", "expired", "OrderStatus.REJECTED", "rejected"}:
                    raise RuntimeError(f"Flatten order {oid} ended as {status}; aborting phase-2 opens")
            pending -= done
            if pending:
                time.sleep(0.5)
        if pending:
            raise RuntimeError(f"Timeout waiting for flatten fills: {sorted(pending)}")

    ordered = pd.DataFrame(legs).sort_values(["phase", "reducing"], ascending=[True, False])
    phase0 = ordered[ordered.phase == 0]
    phase1 = ordered[ordered.phase == 1]
    phase0_ids = _submit_rows(phase0)
    order_ids.extend(phase0_ids)
    if not phase1.empty:
        _wait_filled(phase0_ids)
        order_ids.extend(_submit_rows(phase1))
    return order_ids
