"""
Generate an anonymised demo DB suitable for public deployment.

Reads data/portfolio.db, multiplies every monetary field by SCALE_FACTOR,
keeps quantities as integers, scrambles transfer-ID memos, and writes
data/portfolio_demo.db.

Usage:
    python3 scripts/make_demo_db.py
"""

import shutil
import sqlite3
import random
import string
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "portfolio.db"
DST = ROOT / "data" / "portfolio_demo.db"

# Tweak this if you want the demo numbers to look more or less like yours.
# 0.7 makes everything 70% of real. Quantities are LEFT ALONE (so portfolios
# still feel real) — only dollar/cash fields are scaled.
SCALE_FACTOR = 0.73


def _scramble_id(text: str) -> str:
    """Replace each transfer-ID-looking token with a random new ID."""
    if not text:
        return text
    out = []
    for token in text.split():
        if token.startswith("DDIIRGPC") and len(token) > 12:
            new = "DDIIRGPC" + "".join(
                random.choices(string.ascii_lowercase + string.digits, k=len(token) - 8)
            )
            out.append(new)
        else:
            out.append(token)
    return " ".join(out)


def main():
    if not SRC.exists():
        raise SystemExit(f"Source DB not found: {SRC}")
    if DST.exists():
        DST.unlink()
    shutil.copy2(SRC, DST)
    print(f"Copied {SRC.name} -> {DST.name}")

    conn = sqlite3.connect(DST)
    cur = conn.cursor()

    # Per-table per-column scaling. Quantities are NOT scaled — we keep share
    # counts realistic. Only money/price columns are scaled.
    plan = {
        "nav_snapshots":         ["cash_balance"],
        "cash_events":           ["amount"],
        "positions_snapshots":   ["closing_price", "market_value", "market_value_sgd"],
        "trades":                ["fill_price", "fill_amount", "fees_total", "net_cash_impact"],
        "fund_orders":           ["amount"],
        "live_cash_snapshots":   ["cash", "total_assets_sgd"],
        "live_position_snapshots": ["cost_price", "nominal_price", "market_value", "pl_val"],
        "manual_cash_events":    ["amount"],
        "external_cash_balances":["amount"],
    }

    for table, cols in plan.items():
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            n = cur.fetchone()[0]
        except sqlite3.OperationalError:
            print(f"  skip  {table:25s} (table missing)")
            continue
        if n == 0:
            print(f"  skip  {table:25s} (empty)")
            continue
        for col in cols:
            try:
                cur.execute(f"UPDATE {table} SET {col} = ROUND({col} * ?, 4) WHERE {col} IS NOT NULL",
                            (SCALE_FACTOR,))
            except sqlite3.OperationalError as e:
                print(f"  warn  {table}.{col} not updated: {e}")
        print(f"  scale {table:25s} ({n} rows × {SCALE_FACTOR})")

    # Scramble memos that contain transfer IDs
    for table, col in [
        ("cash_events", "comment"),
        ("manual_cash_events", "memo"),
        ("external_cash_balances", "memo"),
    ]:
        try:
            rows = cur.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL").fetchall()
        except sqlite3.OperationalError:
            continue
        for rid, text in rows:
            new_text = _scramble_id(text or "")
            if new_text != text:
                cur.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (new_text, rid))
        print(f"  scrambled IDs in {table}.{col}  ({len(rows)} rows checked)")

    conn.commit()
    conn.close()

    print(f"\nDone. Demo DB ready at {DST}")
    print(f"Size: {DST.stat().st_size / 1024:.1f} KB")
    print("Next: git add data/portfolio_demo.db && git commit && git push")


if __name__ == "__main__":
    main()
