#!/usr/bin/env bash
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$PROJECT_ROOT/data/dashboard.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" && echo "Stopped dashboard (pid $(cat "$PIDFILE"))."
    rm -f "$PIDFILE"
else
    # Fallback: kill whatever's on port 8501
    PIDS=$(lsof -ti:8501 || true)
    if [ -n "$PIDS" ]; then
        echo "$PIDS" | xargs kill && echo "Killed leftover process(es) on :8501."
    else
        echo "Nothing running."
    fi
fi
