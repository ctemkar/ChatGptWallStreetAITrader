from __future__ import annotations

from dataclasses import dataclass
import os


DEFAULT_UNIVERSE = (
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA",
    "JPM", "V", "MA", "UNH", "XOM", "COST", "HD", "PG", "JNJ", "ABBV",
    "NFLX", "CRM", "AMD", "ORCL", "BAC", "KO", "PEP", "WMT", "CVX",
    "MRK", "CSCO", "ADBE",
)

# Broker rejects sub-$1 fractional notionals.
ALPACA_MIN_NOTIONAL_USD = 1.0

# Skip price-tick micro-rebalances below this notional unless membership/flip.
# Override with env MIN_REBALANCE_NOTIONAL_USD (Chetan 2026-09-24 churn fix).
def min_rebalance_notional_usd() -> float:
    raw = os.getenv("MIN_REBALANCE_NOTIONAL_USD", "25")
    try:
        return float(raw)
    except ValueError:
        return 25.0


MIN_REBALANCE_NOTIONAL_USD = 25.0  # default; call min_rebalance_notional_usd() for live env


def is_membership_or_flip(current_qty: float, target_qty: float) -> bool:
    """True for new entry, full exit, or long<->short flip — never blocked by rebalance floor."""
    eps = 1e-12
    cur = float(current_qty)
    tgt = float(target_qty)
    if abs(cur) <= eps and abs(tgt) > eps:
        return True  # open new name
    if abs(tgt) <= eps and abs(cur) > eps:
        return True  # fully close
    if cur * tgt < 0:
        return True  # short cover through flat / flip sides
    return False


def is_rebalance_delta_actionable(
    current_qty: float,
    target_qty: float,
    delta_qty: float,
    price: float,
    min_notional: float | None = None,
) -> bool:
    """Whether a plan delta should fingerprint/submit.

    Drift below min_notional is skipped. Membership changes (new entry, full
    exit, flip/cover) always pass the rebalance floor (still need Alpaca $1).
    """
    delta = float(delta_qty)
    px = abs(float(price))
    if delta == 0 or px <= 0:
        return False
    notional = abs(delta) * px
    if notional < ALPACA_MIN_NOTIONAL_USD:
        return False
    floor = min_rebalance_notional_usd() if min_notional is None else float(min_notional)
    if is_membership_or_flip(current_qty, target_qty):
        return True
    return notional >= floor


@dataclass(frozen=True)
class StrategyConfig:
    starting_capital: float = 2_200.0
    symbols: tuple[str, ...] = DEFAULT_UNIVERSE
    long_count: int = 5
    short_count: int = 5
    momentum_lookback: int = 126
    momentum_skip: int = 21
    volatility_lookback: int = 20
    # Keep a cash buffer because $2,000 is Alpaca's exact short-selling threshold.
    gross_exposure: float = 0.60
    max_position_weight: float = 0.10
    min_price: float = 5.0
    min_average_dollar_volume: float = 20_000_000.0
    target_annual_volatility: float = 0.12
    max_gross_exposure: float = 0.60
