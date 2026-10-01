"""Alpaca LIVE long/short desk scheduler — Friday 16:30 America/New_York after close.

Safeguards (must all pass before --submit):
- ALPACA_PAPER=false (live account only)
- Unlock env: ALLOW_LIVE_TRADING + AUTO_TRADER_ENABLE
- Explicit arm file logs/ARM_LIVE_SUBMIT (absent => dry-run only / wait)
- Mini submit-idle marker logs/MINI_SUBMIT_IDLE.ok (absent => refuse live)
- Exclusive flock (single process on this host)
- Weekly Friday >=16:30 ET window only (DST via zoneinfo)
- Exchange calendar via Alpaca clock (session must be closed; prefer Friday)
- Refuse if any open orders
- Fresh daily-bar check (latest bar date == last completed US equity session)
- Dry-run CSV artifact first; submit only if actionable + weekly idempotency OK
- Extended-hours limit orders (after-close)

Does not modify strategy code; wraps wallstreet_ls.cli.
Strategy-aligned cadence (README): after U.S. close weekly or monthly; 12-1 momentum.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, time as dtime, date
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
BKK = ZoneInfo("Asia/Bangkok")
UTC = ZoneInfo("UTC")


def now_et() -> datetime:
    return datetime.now(ET)


def now_bkk() -> datetime:
    return datetime.now(BKK)


def log(msg: str, log_path: Path) -> None:
    line = (
        f"[{now_bkk().strftime('%Y-%m-%d %H:%M:%S')} ICT | "
        f"{now_et().strftime('%Y-%m-%d %H:%M:%S')} ET] {msg}"
    )
    # Write once to log file; caller should not also tee stdout into same file.
    print(line, flush=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def week_key(et: datetime) -> str:
    iso = et.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"




def acquire_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.write(f"pid={os.getpid()}\nstarted_ict={now_bkk().isoformat()}\n")
    fh.flush()
    return fh


def ensure_live_env() -> None:
    paper = os.getenv("ALPACA_PAPER", "true").lower()
    if paper != "false":
        raise SystemExit(f"Refusing: ALPACA_PAPER={paper!r} (live desk requires false)")
    if os.getenv("ALLOW_LIVE_TRADING") != "I_UNDERSTAND_THE_RISK":
        raise SystemExit("Refusing: ALLOW_LIVE_TRADING unlock missing")
    if os.getenv("AUTO_TRADER_ENABLE") != "I_UNDERSTAND_AUTOMATION":
        raise SystemExit("Refusing: AUTO_TRADER_ENABLE unlock missing")


def armed(arm_path: Path) -> bool:
    return arm_path.is_file()


def mini_idle_ok(path: Path) -> bool:
    return path.is_file()


def plan_fingerprint(csv_path: Path) -> str:
    import pandas as pd

    df = pd.read_csv(csv_path, index_col=0)
    rows = []
    for sym, row in df.iterrows():
        delta = float(row.delta_qty)
        price = float(row.last_price)
        if abs(delta) * price < 1.0:
            continue
        rows.append(f"{sym}:{delta:.6f}")
    if not rows:
        return "NO_ACTION"
    return hashlib.sha256("|".join(sorted(rows)).encode()).hexdigest()[:16]


def alpaca_clients():
    # Import after dotenv loaded
    sys.path.insert(0, str(ROOT / "src"))
    from wallstreet_ls.alpaca_adapter import clients

    return clients()


def last_completed_session_date(trading_client, et: datetime) -> date:
    """Best-effort last completed regular session date via Alpaca clock."""
    clock = trading_client.get_clock()
    # If market open, last completed session is prior weekday session (approx via next_close/open).
    # If market closed, last session date is typically the date of most recent close.
    next_close = clock.next_close.astimezone(ET)
    next_open = clock.next_open.astimezone(ET)
    is_open = bool(clock.is_open)
    if is_open:
        # Session in progress — bars for "today" may still be incomplete for daily close logic.
        return et.date()  # caller must refuse while open for after-close schedule
    # Closed: if next_open is tomorrow or later today after close, last session is today or previous.
    # When closed after Friday close, next_open is Monday; last session is Friday = next_open.date() - weekend.
    # Safer: use next_close if next_close.date() == et.date() and et > next_close → last = et.date()
    # Alpaca clock when closed: timestamp is now, next_open future, next_close usually equals next session close.
    # Use: if now is after today's scheduled close on a weekday, last session = today; else walk back.
    d = et.date()
    # Walk back to weekday (Mon-Fri); holiday detection separate via is_open/next_open gap.
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    # If next_open is more than ~1 trading day away on a weekday afternoon, could be holiday —
    # treat last completed as the date before next_open's session.
    # When closed Fri evening: next_open Mon → last completed Fri.
    if next_open.date() > et.date():
        # find Friday or last weekday before next_open
        d = next_open.date() - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    return d


def fresh_bars_ok(data_client, symbols: list[str], expect_session: date, log_path: Path) -> bool:
    """Require latest daily bar date >= expect_session for a sample of symbols."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    sample = symbols[:8]
    end = datetime.now(UTC)
    start = end - timedelta(days=10)
    req = StockBarsRequest(
        symbol_or_symbols=sample,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        adjustment=Adjustment.ALL,
        feed=DataFeed.IEX,
    )
    frame = data_client.get_stock_bars(req).df.reset_index()
    if frame.empty:
        log("fresh-bar check FAILED: empty bars", log_path)
        return False
    # timestamp may be tz-aware UTC midnight for session
    latest_by_sym = frame.groupby("symbol")["timestamp"].max()
    ok = True
    for sym, ts in latest_by_sym.items():
        ts = pd_timestamp_to_et_date(ts)
        if ts < expect_session:
            log(f"fresh-bar STALE {sym} latest={ts} expect>={expect_session}", log_path)
            ok = False
    if ok:
        log(f"fresh-bar OK sample={list(latest_by_sym.index)} expect={expect_session}", log_path)
    return ok


def pd_timestamp_to_et_date(ts) -> date:
    if hasattr(ts, "to_pydatetime"):
        dt = ts.to_pydatetime()
    else:
        dt = ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET).date()


def open_orders_count(trading_client) -> int:
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
    return len(trading_client.get_orders(filter=req))


def in_friday_after_close_window(et: datetime, start: dtime, end: dtime) -> bool:
    # Friday == weekday 4
    if et.weekday() != 4:
        return False
    t = et.time()
    return start <= t <= end


def run_cli(submit: bool, extended_hours: bool, output: Path, py: Path):
    cmd = [
        str(py),
        "-m",
        "wallstreet_ls.cli",
        "--strategy",
        "long-short",
        "--output",
        str(output),
    ]
    if extended_hours:
        cmd.append("--extended-hours")
    if submit:
        cmd.append("--submit")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=os.environ.copy(), check=False, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def redact(line: str) -> str:
    if "account_id:" in line:
        return line.split("account_id:")[0] + "account_id: <REDACTED>"
    return line


def maybe_rebalance(args, state: dict, log_path: Path, state_path: Path, plans_dir: Path, py: Path) -> dict:
    et = now_et()
    arm_path = Path(args.arm_file)
    mini_path = Path(args.mini_idle_file)

    if not armed(arm_path):
        log(f"disarmed: missing arm file {arm_path.name} (no live submit)", log_path)
        state["live_enabled"] = False
        state["armed"] = False
        save_state(state_path, state)
        return state

    if not mini_idle_ok(mini_path):
        log(
            f"BLOCKER: missing Mini submit-idle marker {mini_path.name}; refusing live submit",
            log_path,
        )
        state["blocker"] = "Mini submit-idle marker absent"
        state["live_enabled"] = False
        save_state(state_path, state)
        return state

    if not in_friday_after_close_window(et, args.window_start, args.window_end):
        log(
            f"skip: not in Friday after-close window "
            f"(need Fri {args.window_start}-{args.window_end} ET; now {et.strftime('%A %H:%M %Z')})",
            log_path,
        )
        return state

    data_client, trading_client, paper = alpaca_clients()
    if paper:
        log("BLOCKER: clients() reports paper=True; refusing", log_path)
        return state

    clock = trading_client.get_clock()
    if clock.is_open:
        log("skip/refuse: Alpaca clock is_open=True; after-close schedule requires closed session", log_path)
        return state

    # Exchange calendar: if next_open is not the following Monday-ish after Friday, still OK if closed Friday.
    # Refuse if today is marked as a full holiday (next_open still today or clock says closed but not a session day).
    last_sess = last_completed_session_date(trading_client, et)
    if last_sess != et.date() and et.weekday() == 4:
        # Friday but last session isn't today => holiday Friday or early close already long ago
        log(f"skip: last_completed_session={last_sess} != today {et.date()} (holiday/no session?)", log_path)
        return state

    n_open = open_orders_count(trading_client)
    if n_open > 0:
        log(f"refuse: {n_open} open orders present", log_path)
        state["last_result"] = "refused_open_orders"
        save_state(state_path, state)
        return state

    # Fresh bars for completed session
    from wallstreet_ls.config import StrategyConfig

    cfg = StrategyConfig()
    if not fresh_bars_ok(data_client, list(cfg.symbols), last_sess, log_path):
        state["last_result"] = "refused_stale_bars"
        save_state(state_path, state)
        return state

    # Weekly idempotency
    wk = week_key(et)
    if state.get("last_submit_week") == wk and not args.allow_multiple_per_week:
        log(f"skip: weekly idempotency — already submitted {wk}", log_path)
        return state

    stamp = et.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    plan_path = plans_dir / f"live_ls_{stamp}.csv"

    # Dry-run artifact first (extended hours for after-close parity)
    code, out, err = run_cli(submit=False, extended_hours=True, output=plan_path, py=py)
    log(f"dry-run exit={code} artifact={plan_path.name}", log_path)
    for line in (out or "").strip().splitlines()[-25:]:
        log(f"dry| {redact(line)}", log_path)
    for line in (err or "").strip().splitlines()[-15:]:
        log(f"dryERR| {line}", log_path)
    if code != 0:
        state["last_error"] = f"dry-run exit {code}"
        state["last_result"] = "dry_failed"
        save_state(state_path, state)
        return state

    fp = plan_fingerprint(plan_path)
    log(f"plan fingerprint={fp}", log_path)
    state["last_dry_et"] = et.isoformat()
    state["last_fingerprint"] = fp
    state["last_dry_plan"] = plan_path.name

    if fp == "NO_ACTION":
        log("skip: no actionable deltas", log_path)
        state["last_result"] = "no_action"
        # Still mark week reviewed to avoid hammering — optional; keep unmarked so retry if later drift
        save_state(state_path, state)
        return state

    if state.get("last_submit_fingerprint") == fp and state.get("last_submit_week") == wk:
        log("skip: duplicate plan fingerprint for this week", log_path)
        state["last_result"] = "duplicate_plan"
        save_state(state_path, state)
        return state

    if args.dry_run_only:
        log("dry-run-only: not submitting", log_path)
        state["last_result"] = "dry_run_only"
        save_state(state_path, state)
        return state

    log("SUBMITTING live long-short rebalance (--submit --extended-hours)", log_path)
    code, out, err = run_cli(submit=True, extended_hours=True, output=plan_path, py=py)
    log(f"submit exit={code}", log_path)
    submitted_n = None
    for line in (out or "").strip().splitlines()[-40:]:
        log(f"sub| {redact(line)}", log_path)
        if line.startswith("Submitted "):
            try:
                submitted_n = int(line.split()[1])
            except Exception:
                pass
    for line in (err or "").strip().splitlines()[-20:]:
        log(f"subERR| {line}", log_path)

    if code == 0:
        state["last_submit_et"] = et.isoformat()
        state["last_submit_week"] = wk
        state["last_submit_fingerprint"] = fp
        state["last_submit_plan"] = plan_path.name
        state["last_submit_order_count"] = submitted_n
        state["last_result"] = "submitted"
        state.pop("last_error", None)
    else:
        state["last_error"] = f"submit exit {code}"
        state["last_result"] = "submit_failed"
    save_state(state_path, state)
    return state


def parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env.local")
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="Alpaca LIVE long/short Friday after-close desk")
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run-only", action="store_true")
    p.add_argument("--poll-seconds", type=int, default=300)
    p.add_argument("--window-start", default="16:30", help="ET Friday window start HH:MM")
    p.add_argument("--window-end", default="17:30", help="ET Friday window end HH:MM")
    p.add_argument("--allow-multiple-per-week", action="store_true")
    p.add_argument("--state-file", default=str(ROOT / "logs" / "alpaca_live_desk_state.json"))
    p.add_argument("--log-file", default=str(ROOT / "logs" / "alpaca_live_desk.log"))
    p.add_argument("--lock-file", default=str(ROOT / "logs" / "alpaca_live_desk.lock"))
    p.add_argument("--plans-dir", default=str(ROOT / "scheduled_plans"))
    p.add_argument("--arm-file", default=str(ROOT / "logs" / "ARM_LIVE_SUBMIT"))
    p.add_argument("--mini-idle-file", default=str(ROOT / "logs" / "MINI_SUBMIT_IDLE.ok"))
    args = p.parse_args()
    args.window_start = parse_hhmm(args.window_start)
    args.window_end = parse_hhmm(args.window_end)

    log_path = Path(args.log_file)
    state_path = Path(args.state_file)
    lock_path = Path(args.lock_file)
    plans_dir = Path(args.plans_dir)
    plans_dir.mkdir(parents=True, exist_ok=True)

    py = ROOT / ".venv" / "bin" / "python3"
    if not py.exists():
        py = Path(sys.executable)

    lock_fh = acquire_lock(lock_path)
    if lock_fh is None:
        raise SystemExit(f"Another alpaca live desk process holds {lock_path}")

    ensure_live_env()

    is_armed = armed(Path(args.arm_file))
    mini_ok = mini_idle_ok(Path(args.mini_idle_file))
    log(
        f"desk loop start pid={os.getpid()} armed={is_armed} mini_idle_ok={mini_ok} "
        f"window=Fri {args.window_start}-{args.window_end} ET dry_run_only={args.dry_run_only}",
        log_path,
    )

    state = load_state(state_path)
    state["scheduler_started_ict"] = now_bkk().isoformat()
    state["pid"] = os.getpid()
    state["armed"] = is_armed
    state["live_enabled"] = bool(is_armed and mini_ok and not args.dry_run_only)
    state["intended_schedule"] = "weekly Friday 16:30-17:30 America/New_York after close"
    if is_armed and mini_ok:
        state.pop("blocker", None)
    elif not mini_ok:
        state["blocker"] = "Mini submit-idle marker absent"
    save_state(state_path, state)

    save_state(
        ROOT / "logs" / "SOLE_LIVE_SUBMITTER.json",
        {
            "role": "intended_sole_alpaca_live_submitter",
            "host": "grok_bot_computer",
            "machineId": None,
            "path": str(ROOT),
            "armed": is_armed,
            "live_enabled": state.get("live_enabled", False),
            "mini_idle_ok": mini_ok,
            "pid": os.getpid(),
            "updated_ict": now_bkk().isoformat(),
            "schedule": "Friday 16:30-17:30 ET after close",
        },
    )

    try:
        while True:
            try:
                state = load_state(state_path)
                state = maybe_rebalance(args, state, log_path, state_path, plans_dir, py)
            except SystemExit:
                raise
            except Exception as exc:
                log(f"ERROR: {exc}", log_path)
                log(traceback.format_exc(), log_path)
                state = load_state(state_path)
                state["last_error"] = str(exc)
                state["last_error_et"] = now_et().isoformat()
                save_state(state_path, state)
            if args.once:
                break
            time.sleep(max(30, int(args.poll_seconds)))
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()
        log("desk loop stopped", log_path)


if __name__ == "__main__":
    main()
