#!/usr/bin/env bash
#
# One-shot dashboard launcher.
# 1. Refresh data (CSV trades, FX, OpenAPI live snapshot, reconcile drift report).
# 2. Launch Streamlit at http://localhost:8501.
#
# Pre-reqs:
#   - .venv activated  (source .venv/bin/activate)
#   - FUTU_TRD_PWD exported
#   - OpenD running and logged in

set -e
cd "$(dirname "$0")"

echo "=== Refreshing data ==="
python3 run_phase2.py || echo "(phase 2 had a warning, continuing to dashboard anyway)"

echo
echo "=== Launching dashboard at http://localhost:8501 ==="
streamlit run src/app.py
