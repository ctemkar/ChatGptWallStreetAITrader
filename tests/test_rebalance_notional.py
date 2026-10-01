"""Unit checks for MIN_REBALANCE_NOTIONAL_USD micro-churn floor (2026-09-24)."""
from __future__ import annotations

import os

import pandas as pd
import pytest

from wallstreet_ls.config import (
    ALPACA_MIN_NOTIONAL_USD,
    is_membership_or_flip,
    is_rebalance_delta_actionable,
    min_rebalance_notional_usd,
)
from wallstreet_ls.alpaca_adapter import submit_plan


def test_default_floor_is_25(monkeypatch):
    monkeypatch.delenv("MIN_REBALANCE_NOTIONAL_USD", raising=False)
    assert min_rebalance_notional_usd() == 25.0


def test_env_override(monkeypatch):
    monkeypatch.setenv("MIN_REBALANCE_NOTIONAL_USD", "40")
    assert min_rebalance_notional_usd() == 40.0


def test_micro_drift_1_77_skipped(monkeypatch):
    """Reproduce today's post-open crumb size (~$1.77) — must not be actionable."""
    monkeypatch.setenv("MIN_REBALANCE_NOTIONAL_USD", "25")
    # MRK-like: tiny sell after holding ~1.12 shares
    current, target, price = 1.1265, 1.1146, 150.0
    delta = target - current  # ~-0.0119 -> ~$1.78
    assert abs(delta) * price == pytest.approx(1.785, abs=0.01)
    assert not is_rebalance_delta_actionable(current, target, delta, price)


def test_new_entry_allowed_below_floor(monkeypatch):
    monkeypatch.setenv("MIN_REBALANCE_NOTIONAL_USD", "25")
    current, target, price = 0.0, 0.05, 100.0  # $5 new long
    delta = target - current
    assert abs(delta) * price < 25
    assert is_membership_or_flip(current, target)
    assert is_rebalance_delta_actionable(current, target, delta, price)


def test_full_exit_allowed_below_floor(monkeypatch):
    monkeypatch.setenv("MIN_REBALANCE_NOTIONAL_USD", "25")
    current, target, price = 0.04, 0.0, 100.0  # $4 full close
    delta = target - current
    assert abs(delta) * price < 25
    assert is_membership_or_flip(current, target)
    assert is_rebalance_delta_actionable(current, target, delta, price)


def test_flip_allowed(monkeypatch):
    monkeypatch.setenv("MIN_REBALANCE_NOTIONAL_USD", "25")
    current, target, price = 0.1, -1.0, 50.0
    delta = target - current
    assert is_membership_or_flip(current, target)
    assert is_rebalance_delta_actionable(current, target, delta, price)


def test_sub_alpaca_dollar_never_actionable(monkeypatch):
    monkeypatch.setenv("MIN_REBALANCE_NOTIONAL_USD", "25")
    # Even membership under $1 is blocked (broker hard floor)
    assert not is_rebalance_delta_actionable(0.0, 0.005, 0.005, 100.0)  # $0.50
    assert ALPACA_MIN_NOTIONAL_USD == 1.0


class _FakeTradingClient:
    def __init__(self):
        self.submitted = []

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        class R:
            id = f"oid-{len(self.submitted)}"
        return R()


def test_submit_plan_skips_micro_keeps_exit(monkeypatch):
    monkeypatch.setenv("MIN_REBALANCE_NOTIONAL_USD", "25")
    plan = pd.DataFrame(
        [
            # $1.77 drift on existing long — skip
            {
                "symbol": "MRK",
                "current_qty": 1.12,
                "target_qty": 1.108,
                "last_price": 150.0,
                "delta_qty": -0.012,
            },
            # full exit $18 — allow (membership)
            {
                "symbol": "ABBV",
                "current_qty": 0.1,
                "target_qty": 0.0,
                "last_price": 180.0,
                "delta_qty": -0.1,
            },
            # new entry $30 — allow
            {
                "symbol": "ORCL",
                "current_qty": 0.0,
                "target_qty": 0.2,
                "last_price": 150.0,
                "delta_qty": 0.2,
            },
        ]
    ).set_index("symbol")
    # ensure delta_qty matches
    plan["delta_qty"] = plan["target_qty"] - plan["current_qty"]
    client = _FakeTradingClient()
    ids = submit_plan(client, plan, extended_hours=False)
    assert len(ids) == 2
    symbols = [o.symbol for o in client.submitted]
    assert "MRK" not in symbols
    assert set(symbols) == {"ABBV", "ORCL"}
