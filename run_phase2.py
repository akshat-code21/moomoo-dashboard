"""
Phase 2 orchestrator. Runs in order:
    1. csv_trades.ingest_csv(...)  -> trades table
    2. fx.backfill()               -> fx_rates table
    3. openapi_sync.sync()         -> live_*_snapshots
    4. reconcile.reconcile()       -> drift report
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import os

from src.csv_trades import ingest_csv
from src.fund_csv import ingest_fund_csv
from src.fx import backfill
from src.openapi_sync import sync
from src.reconcile import reconcile, compute_drift_summary
from src.db import Session
from src import notify

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "statements" / "raw"
DRIFT_NOTIFY_THRESHOLD_SGD = float(os.environ.get("DRIFT_NOTIFY_THRESHOLD_SGD", "5.0"))


def main():
    # 1. CSV trades — find the most recent History*.csv in statements/raw or in project root
    csvs = sorted(RAW_DIR.glob("History*.csv")) + sorted(ROOT.glob("History*.csv"))
    if csvs:
        latest_csv = csvs[-1]
        print(f"[1/4] Ingesting trades from {latest_csv.name}")
        print(f"     {ingest_csv(latest_csv)}")
    else:
        print("[1/4] No History*.csv found — skipping trades ingest. "
              "Drop one into statements/raw/ to enable mid-month reconciliation.")

    # 1b. Fund orders — any fund*.csv in raw/ or project root
    fund_csvs = sorted(RAW_DIR.glob("fund*.csv")) + sorted(ROOT.glob("fund*.csv"))
    if fund_csvs:
        latest_fund_csv = fund_csvs[-1]
        print(f"\n[1b/4] Ingesting fund orders from {latest_fund_csv.name}")
        print(f"     {ingest_fund_csv(latest_fund_csv)}")
    else:
        print("\n[1b/4] No fund*.csv found — skipping fund orders ingest.")

    # 2. FX
    print("\n[2/4] Backfilling FX rates")
    print(f"     {backfill()}")

    # 3. OpenAPI live sync
    print("\n[3/4] Pulling live snapshot from OpenD")
    try:
        print(f"     {sync()}")
    except Exception as e:
        print(f"     FAIL: {type(e).__name__}: {e}")
        print("     (Is OpenD running and FUTU_TRD_PWD exported?)")
        print("     Skipping reconcile — re-run once OpenD is up.")
        return

    # 4. Reconcile
    print("\n[4/4] Reconciling ledger vs live")
    reconcile()

    # 5. Notify on drift
    with Session() as session:
        summary = compute_drift_summary(session)
    if summary["status"] == "ok" and summary["max_abs_drift_sgd"] > DRIFT_NOTIFY_THRESHOLD_SGD:
        worst_ccy = max(
            summary["details"].items(),
            key=lambda kv: abs(kv[1]["drift_sgd"]),
        )[0]
        worst = summary["details"][worst_ccy]
        notify.send(
            "Moomoo: drift detected",
            f"{worst_ccy} drift S${worst['drift_sgd']:+.2f}. "
            f"Check dashboard or run reconcile to log the event.",
        )


if __name__ == "__main__":
    main()
