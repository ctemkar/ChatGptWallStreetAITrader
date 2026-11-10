"""Weekend hold vs flatten for the live A242 book.

A242 is the ``--strategy a242`` block in the sanitized AlpacaLive-A242 CLI:
4 longs, no shorts, 252-day momentum skipping 3 sessions, 20-day volatility,
gross 1.90 split by the engine into a 0.95 long budget, 40% name cap, 30%
vol target. ``strategy.py`` in that snapshot matches this repo. ``calculate_targets``
is called with every universe name excluded from shorts.

This file does not submit orders, read credentials, or change live code.
It does not treat plan ``sizing_equity`` as account equity.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import math
import tarfile

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
import sys
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))
if str(_ROOT / "research") not in sys.path:
    sys.path.insert(0, str(_ROOT / "research"))

from wallstreet_ls.config import StrategyConfig
from wallstreet_ls.strategy import calculate_targets
from weekend_flat_study import (
    distribution,
    fee_schedule,
    last_session_of_each_week,
    point_in_time_closes,
    regulatory_sell_fee,
)

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "research" / "a242_weekend_output"
PRIOR_PRICES = ROOT / "research" / "weekend_flat_output" / "prices.parquet"
TAR_CANDIDATES = (
    Path("/home/ubuntu/.cursor/projects/workspace/uploads/a242_sanitized.tar_760f.gz"),
    ROOT / "research" / "a242_sanitized.tar.gz",
)
RESEARCH_CAPITAL = 2_200.0
VERIFIED_EQUITY = {
    "2026-10-08": 2_270.23,  # verified close; archive has no A242 plan that day
    "2026-11-09": 2_177.44,
}


def a242_config() -> StrategyConfig:
    """Parameters copied from the sanitized CLI ``--strategy a242`` branch."""
    return StrategyConfig(
        starting_capital=1_000_000_000.0,
        long_count=4,
        short_count=0,
        momentum_lookback=252,
        momentum_skip=3,
        volatility_lookback=20,
        gross_exposure=1.90,
        max_gross_exposure=1.90,
        max_position_weight=0.40,
        min_price=5.0,
        min_average_dollar_volume=20_000_000.0,
        target_annual_volatility=0.30,
    )


def trading_days_between(start_date: date, end_date: date) -> int:
    """Weekdays strictly after start_date through end_date. Matches A242 config.py."""
    n = 0
    d = start_date + timedelta(days=1)
    while d <= end_date:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def _merge_prices(base: pd.DataFrame, extra: pd.DataFrame) -> pd.DataFrame:
    if base is None or base.empty:
        return extra.sort_index()
    combined = pd.concat([base, extra])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    return combined


def _yahoo_symbol(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    import urllib.request

    query = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?period1={int(start.timestamp())}&period2={int(end.timestamp())}"
        "&interval=1d&events=div%7Csplits&includeAdjustedClose=true"
    )
    request = urllib.request.Request(query, headers={"User-Agent": "Mozilla/5.0 a242-weekend-research"})
    with urllib.request.urlopen(request, timeout=60) as response:
        chart = json.load(response)["chart"]["result"][0]
    quote = chart["indicators"]["quote"][0]
    adj = chart["indicators"]["adjclose"][0]["adjclose"]
    rows = []
    for i, ts in enumerate(chart.get("timestamp") or []):
        session = datetime.fromtimestamp(ts, timezone.utc).astimezone(ET).date()
        # Keep a session that has an official open even when Yahoo has not
        # published the close yet. The close stays missing; callers must not
        # fill it forward and treat it as a completed session.
        if quote["open"][i] is None:
            continue
        rows.append({
            "date": pd.Timestamp(session),
            "raw_open": quote["open"][i],
            "raw_close": quote["close"][i],
            "adj_close": adj[i],
            "volume": quote["volume"][i],
        })
    frame = pd.DataFrame(rows).drop_duplicates("date", keep="last").set_index("date").sort_index()
    factor = frame["adj_close"] / frame["raw_close"].replace(0, np.nan)
    # On a session whose close is unpublished, the open is still the raw print.
    frame["adj_open"] = frame["raw_open"] * factor.fillna(1.0)
    dividends = []
    for payload in (chart.get("events") or {}).get("dividends", {}).values():
        when = datetime.fromtimestamp(payload["date"], timezone.utc).astimezone(ET).date()
        dividends.append({"symbol": symbol, "date": pd.Timestamp(when), "amount": float(payload["amount"])})
    frame.attrs["dividends"] = dividends
    return frame


def load_prices(symbols: list[str], refresh_through: str = "2026-11-11") -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    cache = OUTPUT / "prices.parquet"
    div_path = OUTPUT / "dividends.parquet"
    meta_path = OUTPUT / "price_meta.json"
    if cache.exists() and div_path.exists() and meta_path.exists():
        wide = pd.read_parquet(cache)
        panels = {field: wide[field].copy() for field in ("raw_open", "raw_close", "adj_open", "adj_close", "volume")}
        return panels, pd.read_parquet(div_path), json.loads(meta_path.read_text())

    prior = pd.read_parquet(PRIOR_PRICES) if PRIOR_PRICES.exists() else None
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = datetime.fromisoformat(refresh_through).replace(tzinfo=timezone.utc)
    frames = []
    dividends = []
    for symbol in symbols:
        frame = _yahoo_symbol(symbol, start, end)
        dividends.extend(frame.attrs["dividends"])
        frame = frame.drop(columns=[], errors="ignore")
        frame.columns = pd.MultiIndex.from_product([frame.columns, [symbol]])
        frames.append(frame)
    fresh = pd.concat(frames, axis=1).sort_index()
    wide = _merge_prices(prior, fresh) if prior is not None else fresh
    # Keep only requested fields that exist.
    fields = [f for f in ("raw_open", "raw_close", "adj_open", "adj_close", "volume") if f in wide.columns.get_level_values(0)]
    wide = wide.loc[:, fields]
    wide.to_parquet(cache)
    div = pd.DataFrame(dividends)
    div.to_parquet(div_path)
    meta = {
        "source": "Yahoo Finance chart API v8 daily bars, merged onto the prior research cache through 2026-11-06",
        "refreshed_through_request": refresh_through,
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "last_session": str(wide.index.max().date()),
        "first_session": str(wide.index.min().date()),
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    panels = {field: wide[field].copy() for field in fields}
    return panels, div, meta


def signal_weights(adj: pd.DataFrame, raw: pd.DataFrame, volume: pd.DataFrame, asof: pd.Timestamp, config: StrategyConfig) -> pd.Series:
    window = adj.index[adj.index <= asof][-300:]
    scaled = point_in_time_closes(adj.loc[window], raw.loc[window])
    vols = volume.reindex(index=scaled.index, columns=scaled.columns).fillna(0.0)
    targets = calculate_targets(scaled, vols, config, exclude_from_shorts=set(config.symbols))
    return targets["target_weight"].astype(float)


def build_signal_panel(panels: dict[str, pd.DataFrame], symbols: list[str], config: StrategyConfig) -> pd.DataFrame:
    calendar = panels["raw_close"]["SPY"].dropna().index
    calendar = pd.DatetimeIndex(calendar).tz_localize(None).normalize()
    calendar = calendar[(calendar >= pd.Timestamp("2004-01-01")) & ~calendar.duplicated()].sort_values()
    adj = panels["adj_close"].reindex(calendar)[symbols].ffill(limit=3)
    raw = panels["raw_close"].reindex(calendar)[symbols].ffill(limit=3)
    vol = panels["volume"].reindex(calendar)[symbols].fillna(0.0)
    rows = []
    index = []
    required = config.momentum_lookback + 1
    for i, stamp in enumerate(calendar):
        if i + 1 < required:
            continue
        try:
            weights = signal_weights(adj.iloc[: i + 1], raw.iloc[: i + 1], vol.iloc[: i + 1], stamp, config)
        except ValueError:
            continue
        rows.append(weights)
        index.append(stamp)
        if len(index) % 250 == 0:
            print(f"signals {stamp.date()} n={len(index)}", flush=True)
    panel = pd.DataFrame(rows, index=pd.DatetimeIndex(index)).fillna(0.0)
    panel = panel.reindex(columns=symbols).fillna(0.0)
    return panel


def _trade(cash: float, shares: pd.Series, target: pd.Series, price: pd.Series, raw_price: pd.Series, cost_bps: float, day: date, fee_mode: str) -> tuple[float, pd.Series, dict]:
    slip_rate = cost_bps / 10_000.0
    fee = fee_schedule(day, fee_mode)
    new = shares.copy()
    slippage = reg = traded = 0.0
    for sym in shares.index:
        dest = float(target.get(sym, 0.0))
        qty = float(shares[sym])
        delta = dest - qty
        px = float(price.get(sym, np.nan))
        if not np.isfinite(px) or px <= 0 or abs(delta) * px < 0.01:
            continue
        new[sym] = qty + delta
        traded += abs(delta) * px
        if delta > 0:
            slip = delta * px * slip_rate
            cash -= delta * px + slip
            slippage += slip
        else:
            sold = -delta
            slip = sold * px * slip_rate
            sale = sold * px - slip
            cash += sale
            slippage += slip
            raw_px = float(raw_price.get(sym, np.nan))
            real = sold * px / raw_px if np.isfinite(raw_px) and raw_px > 0 else sold
            fee_dollars = regulatory_sell_fee(real, sale, fee)
            cash -= fee_dollars
            reg += fee_dollars
    return cash, new, {"slippage": slippage, "reg": reg, "traded": traded}


def _target_shares(weights: pd.Series, equity: float, price: pd.Series, current: pd.Series, cooldown: dict[str, date], day: date) -> pd.Series:
    """Full-equity weights, with the live micro floor and 3-day re-entry cool-down.

    The weekend round-trip does not call this for the temporary flatten; cool-down
    applies only when the signal itself is reopening a name it closed.
    """
    target = pd.Series(0.0, index=current.index, dtype=float)
    for sym, weight in weights.items():
        if sym not in target.index or weight <= 0:
            continue
        px = float(price.get(sym, np.nan))
        if not np.isfinite(px) or px <= 0:
            target[sym] = float(current[sym])
            continue
        desired = equity * float(weight) / px
        held = float(current[sym])
        if abs(held) <= 1e-12:
            closed = cooldown.get(sym)
            if closed is not None and trading_days_between(closed, day) < 3:
                continue
            target[sym] = desired
            continue
        delta_notional = abs(desired - held) * px
        position_notional = abs(held) * px
        floor = max(5.0, 0.02 * position_notional)
        if delta_notional >= floor:
            target[sym] = desired
        else:
            target[sym] = held
    # Names leaving the signal are full exits and always trade.
    return target


def simulate_a242(
    adj_open: pd.DataFrame,
    adj_close: pd.DataFrame,
    raw_open: pd.DataFrame,
    raw_close: pd.DataFrame,
    signals: pd.DataFrame,
    *,
    flatten: bool,
    cost_bps: float,
    fee_mode: str,
    capital: float,
) -> dict:
    symbols = list(signals.columns)
    calendar = pd.DatetimeIndex(adj_close.index).tz_localize(None).normalize()
    week_last = last_session_of_each_week(calendar)
    cash = float(capital)
    shares = pd.Series(0.0, index=symbols, dtype=float)
    cooldown: dict[str, date] = {}
    pending: pd.Series | None = None
    daily = []
    weekends = []
    pending_obs = None

    def mark(px: pd.Series) -> float:
        value = cash
        for sym, qty in shares.items():
            if qty == 0:
                continue
            price = float(px[sym])
            if not np.isfinite(price):
                raise ValueError(f"missing mark {sym}")
            value += qty * price
        return float(value)

    for i, stamp in enumerate(calendar):
        stamp = pd.Timestamp(stamp).normalize()
        open_px = adj_open.loc[stamp].reindex(symbols).astype(float)
        close_px = adj_close.loc[stamp].reindex(symbols).astype(float)
        raw_open_px = raw_open.loc[stamp].reindex(symbols).astype(float)
        raw_close_px = raw_close.loc[stamp].reindex(symbols).astype(float)
        slip = reg = traded = 0.0
        if pending_obs is not None:
            gap = 0.0
            for sym, qty in pending_obs["shares"].items():
                if qty == 0:
                    continue
                gap += qty * (float(open_px[sym]) - float(pending_obs["close"][sym]))
            base = pending_obs["equity"]
            weekends.append({
                "date": pending_obs["date"],
                "next_date": stamp,
                "gap_pnl": gap,
                "gap_return": gap / base if base else np.nan,
                "gross_notional": pending_obs["gross"],
                "equity": base,
            })
            pending_obs = None
        if pending is not None:
            equity_open = mark(open_px)
            target = _target_shares(pending, equity_open, open_px, shares, cooldown, stamp.date())
            # A signal that no longer holds a name closes it and starts the cool-down.
            for sym in symbols:
                if float(shares[sym]) > 1e-10 and float(target[sym]) <= 1e-12 and float(pending.get(sym, 0.0)) <= 0:
                    cooldown[sym] = stamp.date()
            cash, shares, stats = _trade(cash, shares, target, open_px, raw_open_px, cost_bps, stamp.date(), fee_mode)
            slip += stats["slippage"]
            reg += stats["reg"]
            traded += stats["traded"]
            pending = None
        equity_open = mark(open_px)
        row = signals.loc[stamp] if stamp in signals.index else None
        signal = None
        if row is not None and not row.isna().all():
            signal = row.fillna(0.0)
        is_last = stamp in week_last and i < len(calendar) - 1
        if flatten and is_last:
            zero = pd.Series(0.0, index=symbols)
            cash, shares, stats = _trade(cash, shares, zero, close_px, raw_close_px, cost_bps, stamp.date(), fee_mode)
            slip += stats["slippage"]
            reg += stats["reg"]
            traded += stats["traded"]
            pending = signal if signal is not None else pd.Series(0.0, index=symbols)
        elif signal is not None and i < len(calendar) - 1:
            pending = signal
        equity_close = mark(close_px)
        gross = float((shares.abs() * close_px).sum(skipna=True))
        daily.append({
            "date": stamp,
            "equity_open": equity_open,
            "equity_close": equity_close,
            "gross": gross,
            "gross_weight": gross / equity_close if equity_close else np.nan,
            "slippage": slip,
            "reg": reg,
            "traded": traded,
            "n_names": int((shares > 1e-8).sum()),
        })
        if is_last:
            pending_obs = {
                "date": stamp,
                "shares": shares.copy(),
                "close": close_px.copy(),
                "equity": equity_close,
                "gross": gross,
            }
    daily_df = pd.DataFrame(daily).set_index("date")
    invested = daily_df.index[daily_df["gross"] > 1]
    return {
        "daily": daily_df,
        "weekends": pd.DataFrame(weekends),
        "ending": float(daily_df["equity_close"].iloc[-1]),
        "first": None if len(invested) == 0 else pd.Timestamp(invested[0]),
        "slippage": float(daily_df["slippage"].sum()),
        "reg": float(daily_df["reg"].sum()),
        "traded": float(daily_df["traded"].sum()),
    }


def slice_return(daily: pd.DataFrame, start, end) -> dict:
    sub = daily.loc[(daily.index >= pd.Timestamp(start)) & (daily.index <= pd.Timestamp(end))]
    if len(sub) < 2:
        return {"available": False, "n": int(len(sub))}
    eq0 = float(sub["equity_close"].iloc[0])
    eq1 = float(sub["equity_close"].iloc[-1])
    rets = sub["equity_close"].pct_change().iloc[1:]
    points = [eq0]
    for _, row in sub.iloc[1:].iterrows():
        points.append(float(row.equity_open))
        points.append(float(row.equity_close))
    arr = np.asarray(points, dtype=float)
    dd = float((arr / np.maximum.accumulate(arr) - 1).min())
    n = int(len(sub))
    years = (sub.index[-1] - sub.index[0]).days / 365.25
    std = float(rets.std(ddof=1)) if len(rets) > 1 else np.nan
    sharpe = None
    if n >= 252 and std > 0:
        sharpe = float(rets.mean() / std * np.sqrt(252))
    return {
        "available": True,
        "start": str(sub.index[0].date()),
        "end": str(sub.index[-1].date()),
        "n": n,
        "equity_start": eq0,
        "equity_end": eq1,
        "pnl": eq1 - eq0,
        "total_return": eq1 / eq0 - 1,
        "max_drawdown": dd,
        "sharpe_zero_rf": sharpe,
        "years": years,
        "mean_gross_weight": float(sub["gross_weight"].mean()),
    }


def _parse_plan_name(name: str) -> datetime:
    stamp = name.replace("live_ls_cont_", "").replace(".csv", "")
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(ET)


def load_plans(tar_path: Path | None) -> pd.DataFrame:
    cached = OUTPUT / "plan_snapshots.parquet"
    if cached.exists() and tar_path is None:
        return pd.read_parquet(cached)
    if tar_path is None or not tar_path.exists():
        if cached.exists():
            return pd.read_parquet(cached)
        raise FileNotFoundError("A242 plan archive is not available and no snapshot cache exists")
    rows = []
    with tarfile.open(tar_path, "r:gz") as archive:
        for member in archive:
            if not member.name.endswith(".csv") or "scheduled_plans/" not in member.name:
                continue
            when = _parse_plan_name(Path(member.name).name)
            frame = pd.read_csv(archive.extractfile(member))
            if "symbol" not in frame.columns:
                frame = frame.rename(columns={frame.columns[0]: "symbol"})
            for _, row in frame.iterrows():
                rows.append({
                    "et": when,
                    "session": pd.Timestamp(when.date()),
                    "minutes": when.hour * 60 + when.minute,
                    "symbol": row["symbol"],
                    "target_weight": float(row["target_weight"]),
                    "last_price": float(row["last_price"]),
                    "current_qty": float(row["current_qty"]),
                    "target_qty": float(row["target_qty"]),
                })
    out = pd.DataFrame(rows)
    out.to_parquet(cached)
    return out


def evening_snapshots(plans: pd.DataFrame) -> pd.DataFrame:
    """Last plan of each ET date, preferring one at or after 16:00."""
    chosen = []
    for session, frame in plans.groupby("session"):
        after = frame[frame["minutes"] >= 16 * 60]
        use = after if len(after) else frame
        last_time = use["et"].max()
        snap = use[use["et"] == last_time]
        for _, row in snap.iterrows():
            chosen.append({
                "session": pd.Timestamp(session),
                "et": row["et"],
                "symbol": row["symbol"],
                "current_qty": row["current_qty"],
                "target_weight": row["target_weight"],
                "last_price": row["last_price"],
                "after_close": bool(row["minutes"] >= 16 * 60),
            })
    return pd.DataFrame(chosen)


def live_weekend_table(snapshots: pd.DataFrame, raw_open: pd.DataFrame, raw_close: pd.DataFrame, dividends: pd.DataFrame) -> pd.DataFrame:
    # Completed sessions have a close. The next open may be a session whose
    # close has not been published yet.
    completed = pd.DatetimeIndex(raw_close["SPY"].dropna().index).tz_localize(None).normalize()
    opens = pd.DatetimeIndex(raw_open["SPY"].dropna().index).tz_localize(None).normalize()
    calendar = completed.union(opens).sort_values()
    week_last = last_session_of_each_week(completed)
    sessions = list(calendar)
    loc = {pd.Timestamp(d): i for i, d in enumerate(sessions)}
    rows = []
    for session, frame in snapshots.groupby("session"):
        session = pd.Timestamp(session).normalize()
        if session not in week_last or session not in loc:
            continue
        i = loc[session]
        if i + 1 >= len(sessions):
            continue
        nxt = sessions[i + 1]
        gross = 0.0
        gap = 0.0
        names = []
        for _, row in frame.iterrows():
            sym = row["symbol"]
            qty = float(row["current_qty"])
            if abs(qty) < 1e-8 or sym not in raw_close.columns:
                continue
            px0 = float(raw_close[sym].get(session, np.nan))
            px1 = float(raw_open[sym].get(nxt, np.nan))
            if not np.isfinite(px0) or not np.isfinite(px1):
                continue
            cash_div = 0.0
            if len(dividends):
                hit = dividends[(dividends["symbol"] == sym) & (dividends["date"] > session) & (dividends["date"] <= nxt)]
                cash_div = float(hit["amount"].sum()) if len(hit) else 0.0
            gap += qty * ((px1 + cash_div) - px0)
            gross += abs(qty) * px0
            names.append(sym)
        rows.append({
            "friday": session,
            "next_open": nxt,
            "snapshot_et": str(frame["et"].iloc[0]),
            "after_close": bool(frame["after_close"].iloc[0]),
            "names": ",".join(sorted(set(names))),
            "gap_pnl": gap,
            "gross_notional": gross,
            "gap_return_on_gross": gap / gross if gross else np.nan,
        })
    return pd.DataFrame(rows)


def json_ready(value):
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def save_figures(path: Path, curves: dict[str, pd.Series], gaps: pd.Series, dollars: list[dict]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    for label, series in curves.items():
        base = series.dropna()
        ax.plot(base.index, base / float(base.iloc[0]), label=label, linewidth=1.3)
    ax.set_title("A242 research account, 5 bps per side")
    ax.set_ylabel("Growth of $1 at first invested close")
    ax.legend(frameon=False)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path / "a242_equity_5bps.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    vals = gaps.dropna() * 10_000
    ax.hist(vals, bins=40, color="#3c5a73", edgecolor="white")
    ax.axvline(0, color="black", linewidth=0.8)
    ax.axvline(vals.mean(), color="#9a3412", linewidth=1.2, label=f"mean {vals.mean():.2f} bps")
    ax.set_title("A242 hold-book gap, last close to next open")
    ax.set_xlabel("Account return (bps)")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(path / "a242_gap_hist.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    labels = [f"{row['bps']} bps" for row in dollars]
    ax.bar(np.arange(len(labels)) - 0.18, [row["hold_pnl"] for row in dollars], width=0.36, label="Hold")
    ax.bar(np.arange(len(labels)) + 0.18, [row["flat_pnl"] for row in dollars], width=0.36, label="Flatten")
    ax.set_xticks(np.arange(len(labels)), labels)
    ax.set_ylabel("Profit on the $2,200 research account ($)")
    ax.set_title("A242 full history, dollars versus staying in cash at $2,200")
    ax.axhline(0, color="black", linewidth=0.8)
    ax.legend(frameon=False)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path / "a242_dollar_pnl.png", dpi=120)
    plt.close(fig)


def main() -> None:
    config = a242_config()
    symbols = list(config.symbols)
    panels, dividends, price_meta = load_prices(symbols + ["SPY"])
    calendar = panels["raw_close"]["SPY"].dropna().index
    calendar = pd.DatetimeIndex(pd.to_datetime(calendar)).tz_localize(None).normalize()
    calendar = calendar[~calendar.duplicated()].sort_values()
    signal_path = OUTPUT / "a242_signals.parquet"
    if signal_path.exists():
        signals = pd.read_parquet(signal_path)
    else:
        signals = build_signal_panel(panels, symbols, config)
        signals.to_parquet(signal_path)
    # Align prices to the SPY session calendar. Signals stay on their own dates.
    book = {
        key: panels[key].reindex(calendar)[symbols].ffill(limit=3)
        for key in ("adj_open", "adj_close", "raw_open", "raw_close")
    }
    book["volume"] = panels["volume"].reindex(calendar)[symbols].fillna(0.0)
    # Carry the last good signal across a session where the engine could not
    # form a book. Leading rows stay missing so the account is still cash.
    signals = signals.reindex(calendar).ffill()

    # Nov 6 validation against the evening plan, checked after plans load.
    runs = {}
    summaries = {}
    for flatten in (False, True):
        for bps, fee_mode in ((0, "none"), (5, "none"), (10, "none"), (5, "disclosed")):
            key = f"{'flat' if flatten else 'hold'}_bps{bps}_fee{fee_mode}"
            result = simulate_a242(
                book["adj_open"], book["adj_close"], book["raw_open"], book["raw_close"], signals,
                flatten=flatten, cost_bps=bps, fee_mode=fee_mode, capital=RESEARCH_CAPITAL,
            )
            runs[key] = result
            first = result["first"]
            daily = result["daily"]
            summaries[key] = {
                "ending": result["ending"],
                "pnl_vs_2200": result["ending"] - RESEARCH_CAPITAL,
                "first_invested": None if first is None else str(first.date()),
                "slippage": result["slippage"],
                "reg": result["reg"],
                "traded": result["traded"],
                "full": slice_return(daily, first, daily.index[-1]) if first is not None else {"available": False},
                "holdout": slice_return(daily, "2023-01-01", "2026-10-07"),
                "live_scale_window": slice_return(daily, "2026-10-08", daily.index[-1]),
            }
            print(key, "end", round(result["ending"], 2), "pnl", round(result["ending"] - RESEARCH_CAPITAL, 2), flush=True)

    hold0 = runs["hold_bps0_feenone"]
    gap_stats = {
        "full": distribution(hold0["weekends"]["gap_return"]),
        "holdout": distribution(hold0["weekends"].loc[(hold0["weekends"]["date"] >= "2023-01-01") & (hold0["weekends"]["date"] <= "2026-10-07"), "gap_return"]),
        "from_2026_10_08": distribution(hold0["weekends"].loc[hold0["weekends"]["date"] >= "2026-10-08", "gap_return"]),
    }

    tar_path = next((path for path in TAR_CANDIDATES if path.exists()), None)
    plans = load_plans(tar_path)
    snaps = evening_snapshots(plans)
    live = live_weekend_table(
        snaps,
        panels["raw_open"],
        panels["raw_close"],
        dividends if isinstance(dividends, pd.DataFrame) else pd.DataFrame(),
    )
    live.to_parquet(OUTPUT / "live_weekend_gaps.parquet")
    # Nov 6 signal versus the evening plan's target weights.
    nov6 = snaps[snaps["session"] == pd.Timestamp("2026-11-06")]
    signal_nov6 = signals.loc[pd.Timestamp("2026-11-06")]
    signal_nov6 = signal_nov6[signal_nov6 > 0]
    plan_nov6 = nov6.set_index("symbol")["target_weight"]
    plan_nov6 = plan_nov6[plan_nov6 > 0]
    both = sorted(set(signal_nov6.index) | set(plan_nov6.index))
    nov6_compare = {
        sym: {
            "signal": float(signal_nov6.get(sym, 0.0)),
            "plan_target": float(plan_nov6.get(sym, 0.0)),
        }
        for sym in both
    }

    live_costs = []
    for bps in (0, 5, 10):
        gap = float(live["gap_pnl"].sum()) if len(live) else 0.0
        spread = 0.0
        reg = 0.0
        for _, row in live.iterrows():
            spread += 2 * (bps / 10_000.0) * float(row["gross_notional"])
        for _, row in live.iterrows():
            fee = fee_schedule(pd.Timestamp(row["friday"]).date(), "disclosed")
            session = pd.Timestamp(row["friday"])
            snap = snaps[(snaps["session"] == session)]
            for _, pos in snap.iterrows():
                qty = abs(float(pos["current_qty"]))
                if qty <= 0:
                    continue
                px = float(book["raw_close"][pos["symbol"]].get(session, np.nan))
                if not np.isfinite(px):
                    continue
                reg += regulatory_sell_fee(qty, qty * px * (1 - bps / 10_000.0), fee)
        flat_minus_hold = -gap - spread
        live_costs.append({
            "bps_per_side": bps,
            "weekend_gap_pnl_if_held": gap,
            "round_trip_spread": spread,
            "regulatory_on_flatten_sale": reg,
            "flat_minus_hold_after_spread": flat_minus_hold,
            "flat_minus_hold_after_spread_and_reg_fee": flat_minus_hold - reg,
        })

    dollars = []
    for bps in (0, 5, 10):
        hold = summaries[f"hold_bps{bps}_feenone"]
        flat = summaries[f"flat_bps{bps}_feenone"]
        dollars.append({
            "bps": bps,
            "hold_pnl": hold["pnl_vs_2200"],
            "flat_pnl": flat["pnl_vs_2200"],
            "flat_minus_hold": flat["pnl_vs_2200"] - hold["pnl_vs_2200"],
        })

    curves = {
        "Hold": runs["hold_bps5_feenone"]["daily"]["equity_close"].loc[runs["hold_bps5_feenone"]["first"]:],
        "Flatten": runs["flat_bps5_feenone"]["daily"]["equity_close"].loc[runs["flat_bps5_feenone"]["first"]:],
    }
    save_figures(OUTPUT, curves, hold0["weekends"]["gap_return"], dollars)

    payload = {
        "identity": {
            "strategy": "a242",
            "source": "sanitized AlpacaLive-A242 snapshot collected 2026-11-08, base git 09b6fe0 plus uncommitted CLI/config/adapter/runner",
            "parameters": {
                "long_count": 4,
                "short_count": 0,
                "momentum_lookback": 252,
                "momentum_skip": 3,
                "volatility_lookback": 20,
                "gross_exposure_config": 1.90,
                "engine_long_budget": 0.95,
                "engine_note": "calculate_targets sets each side's budget to gross/2. With shorts excluded, only the 0.95 long budget is invested. Live Nov 6 target weights sum to 0.95.",
                "max_position_weight": 0.40,
                "target_annual_volatility": 0.30,
                "adjustment": "Alpaca Adjustment.ALL on a rolling ~400-day window. This study uses Yahoo adjusted OHLC, rescaled so the decision close equals the tradeable close.",
                "orders": "Continuous runner. Through 2026-10-31 it could submit extended-hours limits when the cash session was closed. The Nov 1 gate refuses submits outside regular hours. Regular-hours orders are market DAY orders, not market-on-close.",
                "rebalance_floor": "max($5, 2% of position notional); entries, exits, and flips always pass",
                "reentry_cooldown_trading_days": 3,
            },
            "not_used": "Plan sizing_equity is not account equity. Verified marks are 2026-10-08 close $2,270.23 and 2026-11-09 $2,177.44.",
        },
        "price_meta": price_meta,
        "signal_validation_2026_11_06": nov6_compare,
        "history_on_2200": summaries,
        "history_dollar_difference": dollars,
        "history_gap_distribution_hold_0bps": gap_stats,
        "live_observed": {
            "plan_rows": int(len(plans)),
            "first_plan_et": str(plans["et"].min()),
            "last_plan_et": str(plans["et"].max()),
            "events_start": "2026-10-18 13:34 ET. The 2026-10-10 runner backup still called --strategy a73. No A242 plan exists for 2026-10-08.",
            "weekends": live.to_dict(orient="records"),
            "costs": live_costs,
            "verified_equity_change_oct8_to_nov9": VERIFIED_EQUITY["2026-11-09"] - VERIFIED_EQUITY["2026-10-08"],
        },
    }
    (OUTPUT / "results.json").write_text(json.dumps(json_ready(payload), indent=2))
    print("wrote", OUTPUT / "results.json", flush=True)


if __name__ == "__main__":
    main()
