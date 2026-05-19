#!/usr/bin/env bash
#
# Wrapper for scheduled (launchd / cron) runs of run_phase2.py.
# - Loads secrets from .env (FUTU_TRD_PWD etc.)
# - Activates the venv
# - Runs run_phase2.py
# - All output goes to data/refresh.log
#
# Manual invocation:
#     bash scripts/run_refresh.sh

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p data
LOG="$PROJECT_ROOT/data/refresh.log"

{
    echo "================================================================"
    echo "Refresh run at $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "================================================================"

    # Load .env if present
    if [ -f .env ]; then
        set -a
        # shellcheck disable=SC1091
        source .env
        set +a
        echo "Loaded .env"
    else
        echo "WARNING: no .env file found at $PROJECT_ROOT/.env"
        echo "Copy .env.example to .env and add your FUTU_TRD_PWD."
    fi

    # Activate venv
    if [ -f .venv/bin/activate ]; then
        # shellcheck disable=SC1091
        source .venv/bin/activate
    else
        echo "ERROR: .venv not found. Create it with: python3 -m venv .venv"
        exit 1
    fi

    python3 run_phase2.py
} >> "$LOG" 2>&1

# Exit code from python3 above is preserved by `set -e`
