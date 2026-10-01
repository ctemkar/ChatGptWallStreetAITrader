"""Alpaca LIVE continuous strategy-driven desk (Grok Bot sole submitter).

Continuous evaluate loop: strategy computes the plan each tick; --submit only
when the plan is actionable AND the fingerprint differs from the last submit.
No Friday-only or after-close-only gate (user FINAL 2026-09-17 ~04:20 ICT).

Hard safety (all required):
- ALPACA_PAPER=false + ALLOW_LIVE_TRADING + AUTO_TRADER_ENABLE unlocks
- logs/ARM_LIVE_SUBMIT present
- logs/MINI_SUBMIT_IDLE.ok present (Mini submit-idle)
- exclusive flock (single process)
- refuse if open orders present
- skip NO_ACTION / identical fingerprints
- full logging; no secrets printed

Wraps wallstreet_ls.cli; does not modify strategy code.
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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
BKK = ZoneInfo("Asia/Bangkok")


def now_et() -> datetime:
    return datetime.now(ET)


def now_bkk() -> datetime:
    return datetime.now(BKK)


def log(msg: str, log_path: Path) -> None:
    line = (
        f"[{now_bkk().strftime('%Y-%m-%d %H:%M:%S')} ICT | "
        f"{now_et().strftime('%Y-%m-%d %H:%M:%S')} ET] {msg}"
    )
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


def acquire_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.write(f"pid={os.getpid()}\nstarted_ict={now_bkk().isoformat()}\nmode=continuous\n")
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

    sys.path.insert(0, str(ROOT / "src"))
    from wallstreet_ls.config import is_rebalance_delta_actionable

    df = pd.read_csv(csv_path, index_col=0)
    rows = []
    for sym, row in df.iterrows():
        delta = float(row.delta_qty)
        price = float(row.last_price)
        current = float(row.current_qty)
        target = float(row.target_qty)
        # Shared $25 floor (MIN_REBALANCE_NOTIONAL_USD); membership/flip always actionable.
        if not is_rebalance_delta_actionable(current, target, delta, price):
            continue
        rows.append(f"{sym}:{delta:.6f}")
    if not rows:
        return "NO_ACTION"
    return hashlib.sha256("|".join(sorted(rows)).encode()).hexdigest()[:16]


def alpaca_clients():
    sys.path.insert(0, str(ROOT / "src"))
    from wallstreet_ls.alpaca_adapter import clients

    return clients()


def open_orders_count(trading_client) -> int:
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
    return len(trading_client.get_orders(filter=req))


def market_is_open(trading_client) -> bool:
    try:
        return bool(trading_client.get_clock().is_open)
    except Exception:
        return False


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
    proc = subprocess.run(
        cmd, cwd=str(ROOT), env=os.environ.copy(), check=False, capture_output=True, text=True
    )
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

    state["armed"] = True
    state["live_enabled"] = not args.dry_run_only
    state.pop("blocker", None)

    data_client, trading_client, paper = alpaca_clients()
    if paper:
        log("BLOCKER: clients() reports paper=True; refusing", log_path)
        state["last_result"] = "refused_paper_mode"
        save_state(state_path, state)
        return state

    n_open = open_orders_count(trading_client)
    if n_open > 0:
        log(f"refuse: {n_open} open orders present", log_path)
        state["last_result"] = "refused_open_orders"
        state["open_orders"] = n_open
        save_state(state_path, state)
        return state

    is_open = market_is_open(trading_client)
    # Market hours => market orders; closed => extended-hours limits (no Friday gate).
    use_ext = not is_open
    stamp = et.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    plan_path = plans_dir / f"live_ls_cont_{stamp}.csv"

    code, out, err = run_cli(submit=False, extended_hours=use_ext, output=plan_path, py=py)
    log(
        f"dry-run exit={code} artifact={plan_path.name} market_open={is_open} ext_hours={use_ext}",
        log_path,
    )
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
    state["last_dry_ict"] = now_bkk().isoformat()
    state["last_fingerprint"] = fp
    state["last_dry_plan"] = plan_path.name
    state["market_open_at_eval"] = is_open

    if fp == "NO_ACTION":
        log("skip: no actionable deltas (strategy wants no trade)", log_path)
        state["last_result"] = "no_action"
        save_state(state_path, state)
        return state

    if state.get("last_submit_fingerprint") == fp:
        log("skip: identical plan fingerprint to last submit (no-op)", log_path)
        state["last_result"] = "duplicate_plan"
        save_state(state_path, state)
        return state

    if args.dry_run_only:
        log("dry-run-only: actionable plan but not submitting", log_path)
        state["last_result"] = "dry_run_only_actionable"
        save_state(state_path, state)
        return state

    log(
        f"SUBMITTING live long-short (strategy actionable fp={fp}; "
        f"{'extended-hours limits' if use_ext else 'RTH market orders'})",
        log_path,
    )
    code, out, err = run_cli(submit=True, extended_hours=use_ext, output=plan_path, py=py)
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
        state["last_submit_ict"] = now_bkk().isoformat()
        state["last_submit_fingerprint"] = fp
        state["last_submit_plan"] = plan_path.name
        state["last_submit_order_count"] = submitted_n
        state["last_result"] = "submitted"
        state["orders_submitted_this_changeover"] = int(
            state.get("orders_submitted_this_changeover") or 0
        ) + (submitted_n or 0)
        state.pop("last_error", None)
    else:
        state["last_error"] = f"submit exit {code}"
        state["last_result"] = "submit_failed"
    save_state(state_path, state)
    return state


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env.local")
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

    p = argparse.ArgumentParser(
        description="Alpaca LIVE continuous strategy-driven desk (no Friday/after-hours-only gate)"
    )
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run-only", action="store_true")
    p.add_argument("--poll-seconds", type=int, default=120)
    p.add_argument("--state-file", default=str(ROOT / "logs" / "alpaca_live_desk_state.json"))
    p.add_argument("--log-file", default=str(ROOT / "logs" / "alpaca_live_continuous.log"))
    p.add_argument("--lock-file", default=str(ROOT / "logs" / "alpaca_live_continuous.lock"))
    p.add_argument("--plans-dir", default=str(ROOT / "scheduled_plans"))
    p.add_argument("--arm-file", default=str(ROOT / "logs" / "ARM_LIVE_SUBMIT"))
    p.add_argument("--mini-idle-file", default=str(ROOT / "logs" / "MINI_SUBMIT_IDLE.ok"))
    args = p.parse_args()

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
        raise SystemExit(f"Another continuous live desk process holds {lock_path}")

    # Also refuse if the old weekly desk is still running (shared intent)
    weekly_lock = ROOT / "logs" / "alpaca_live_desk.lock"
    if weekly_lock.exists():
        try:
            wfh = weekly_lock.open("w")
            try:
                fcntl.flock(wfh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(wfh.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                wfh.close()
                raise SystemExit("REFUSE: weekly run_alpaca_live_desk still holds lock")
            wfh.close()
        except SystemExit:
            raise
        except Exception:
            pass

    ensure_live_env()

    is_armed = armed(Path(args.arm_file))
    mini_ok = mini_idle_ok(Path(args.mini_idle_file))
    log(
        f"CONTINUOUS desk start pid={os.getpid()} armed={is_armed} mini_idle_ok={mini_ok} "
        f"poll={args.poll_seconds}s dry_run_only={args.dry_run_only} "
        f"mode=strategy-driven-continuous (no Friday/after-hours-only gate)",
        log_path,
    )

    state = load_state(state_path)
    state["scheduler_started_ict"] = now_bkk().isoformat()
    state["pid"] = os.getpid()
    state["armed"] = is_armed
    state["live_enabled"] = bool(is_armed and mini_ok and not args.dry_run_only)
    state["intended_schedule"] = "continuous strategy-driven evaluate+submit (poll {}s)".format(
        args.poll_seconds
    )
    state["cadence_note"] = (
        "USER FINAL 2026-09-17 ~04:20 ICT: continuous + auto-submit whenever strategy wants. "
        "Standing Always allow. No Friday-only / after-close-only gate. "
        "Submit iff actionable fingerprint change. Sole LIVE submitter = Grok Bot box."
    )
    state["authorization_note"] = (
        "User 2026-09-16 ~23:52 ICT Always allow + auto-submit ON; "
        "User FINAL 2026-09-17 ~04:20 ICT continuous strategy-driven submit."
    )
    state["user_decision_continuous_ict"] = "2026-09-17T04:20:00+07:00"
    state["mode"] = "continuous"
    state.pop("next_window_note", None)
    state["orders_submitted_this_changeover"] = 0
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
            "schedule": f"continuous strategy-driven poll={args.poll_seconds}s",
            "mode": "continuous",
            "script": "scripts/run_alpaca_live_continuous.py",
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
        log("continuous desk loop stopped", log_path)


if __name__ == "__main__":
    main()
