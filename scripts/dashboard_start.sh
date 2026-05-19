#!/usr/bin/env bash
#
# Start the Streamlit dashboard in the background.
# Output goes to data/dashboard.log; PID stored in data/dashboard.pid.
# Re-running tears down the previous instance first (idempotent).

set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
mkdir -p data

PIDFILE="$PROJECT_ROOT/data/dashboard.pid"
LOG="$PROJECT_ROOT/data/dashboard.log"

# Kill any old instance first
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Stopping old dashboard (pid $(cat "$PIDFILE"))..."
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    sleep 1
fi
# Also clean up any orphan streamlit on port 8501
lsof -ti:8501 | xargs kill 2>/dev/null || true

# Load .env (for FUTU_TRD_PWD)
if [ -f .env ]; then
    set -a; source .env; set +a
fi

# Activate venv
# shellcheck disable=SC1091
source .venv/bin/activate

# Launch streamlit detached. --server.address controls who can reach it:
#   127.0.0.1   only this Mac (default, most secure)
#   0.0.0.0     any device on your LAN (your phone on same WiFi)
ADDR="${DASHBOARD_BIND:-127.0.0.1}"
PORT="${DASHBOARD_PORT:-8501}"

nohup streamlit run src/app.py \
    --server.address "$ADDR" \
    --server.port "$PORT" \
    --server.headless true \
    --browser.gatherUsageStats false \
    >>"$LOG" 2>&1 &
echo $! > "$PIDFILE"

sleep 2
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Dashboard up. PID $(cat "$PIDFILE")."
    echo "Local:   http://localhost:$PORT"
    if [ "$ADDR" = "0.0.0.0" ]; then
        IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "?")
        echo "LAN:     http://$IP:$PORT  (your phone on same WiFi can use this)"
    fi
    echo "Log:     tail -f $LOG"
    echo "Stop:    bash scripts/dashboard_stop.sh"
else
    echo "Failed to start. Check $LOG"
    exit 1
fi
