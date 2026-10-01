#!/usr/bin/env bash
# Launcher for Alpaca LIVE long/short desk (Grok Bot).
# Cadence: weekly Friday 16:30-17:30 America/New_York after close.
# Refuses to start live-armed without ARM + Mini-idle markers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
PID_FILE="$LOG_DIR/alpaca_live_desk.pid"
LAUNCH_LOG="$LOG_DIR/alpaca_live_desk_launch.log"
ARM="$LOG_DIR/ARM_LIVE_SUBMIT"
MINI="$LOG_DIR/MINI_SUBMIT_IDLE.ok"

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

MODE="${1:-status}"

# status/stop must not be blocked by the already-running gate
case "$MODE" in
  status)
    echo "arm_file=$([[ -f $ARM ]] && echo present || echo ABSENT)"
    echo "mini_idle=$([[ -f $MINI ]] && echo present || echo ABSENT)"
    echo "pid_file=$([[ -f $PID_FILE ]] && cat "$PID_FILE" || echo none)"
    pgrep -af 'run_alpaca_live_desk.py' || echo 'no scheduler process'
    exit 0
    ;;
  stop)
    if [[ -f "$PID_FILE" ]]; then
      kill "$(cat "$PID_FILE")" 2>/dev/null || true
      rm -f "$PID_FILE"
    fi
    pkill -f 'run_alpaca_live_desk.py' 2>/dev/null || true
    rm -f "$LOG_DIR/alpaca_live_desk.lock"
    echo "[$(ts)] stopped" | tee -a "$LAUNCH_LOG"
    exit 0
    ;;
esac

if [[ -f "$PID_FILE" ]]; then
  old="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${old}" ]] && kill -0 "$old" 2>/dev/null; then
    if ps -p "$old" -o args= 2>/dev/null | grep -q 'run_alpaca_live_desk.py'; then
      echo "[$(ts)] already running pid=$old" | tee -a "$LAUNCH_LOG"
      exit 0
    fi
  fi
fi

# Only treat a real python scheduler process as a dual-launcher risk (ignore grep/self cmdline).
if pgrep -f '/python3? .*(scripts/)?run_momentum_scheduler\.py' >/dev/null 2>&1; then
  echo "[$(ts)] REFUSE: run_momentum_scheduler.py present — dual launcher risk" | tee -a "$LAUNCH_LOG"
  pgrep -af 'run_momentum_scheduler\.py' | tee -a "$LAUNCH_LOG" || true
  exit 1
fi

case "$MODE" in
  dry-loop)
    # Always-on loop but never submits (no arm needed); for monitoring only
    export ALPACA_PAPER=false
    export ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK
    export AUTO_TRADER_ENABLE=I_UNDERSTAND_AUTOMATION
    export PYTHONUNBUFFERED=1
    PY="$ROOT/.venv/bin/python3"
    nohup "$PY" "$ROOT/scripts/run_alpaca_live_desk.py" \
      --dry-run-only \
      --poll-seconds 300 \
      --window-start 16:30 \
      --window-end 17:30 \
      >> "$LOG_DIR/alpaca_live_desk_stdout.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "[$(ts)] launched DRY-ONLY pid=$(cat "$PID_FILE")" | tee -a "$LAUNCH_LOG"
    ;;
  live)
    if [[ ! -f "$ARM" ]]; then
      echo "[$(ts)] REFUSE live: missing $ARM" | tee -a "$LAUNCH_LOG"
      exit 2
    fi
    if [[ ! -f "$MINI" ]]; then
      echo "[$(ts)] REFUSE live: missing $MINI (Mini submit-idle not confirmed)" | tee -a "$LAUNCH_LOG"
      exit 3
    fi
    export ALPACA_PAPER=false
    export ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK
    export AUTO_TRADER_ENABLE=I_UNDERSTAND_AUTOMATION
    export PYTHONUNBUFFERED=1
    PY="$ROOT/.venv/bin/python3"
    nohup "$PY" "$ROOT/scripts/run_alpaca_live_desk.py" \
      --poll-seconds 300 \
      --window-start 16:30 \
      --window-end 17:30 \
      >> "$LOG_DIR/alpaca_live_desk_stdout.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "[$(ts)] launched LIVE-ARMED weekly pid=$(cat "$PID_FILE")" | tee -a "$LAUNCH_LOG"
    ;;
  *)
    echo "usage: $0 {status|dry-loop|live|stop}"
    exit 64
    ;;
esac
