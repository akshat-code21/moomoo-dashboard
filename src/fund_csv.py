"""
Parse the Moomoo fund-orders CSV (`fund accounts.csv`) into the fund_orders
table.

CSV columns:
    Order type        Subscribe | Redeem
    Status            Completed | Submitted | Terminated
    Product Name      e.g. 'LionGlobal Singapore Trust Fund'
    Amount/Units      e.g. '330.77USD' or '16.0944Units' or '300.00SGD'
    Currency          USD / SGD / etc. (same as suffix on amount when present)
    Order Time        'YYYY/MM/DD HH:MM:SS'

Usage:
    python3 -m src.fund_csv /path/to/fund\ accounts.csv
"""

from __future__ import annotations
import csv
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

from src.db import Session, create_all, FundOrder


# Match either '330.77USD' or '16.0944Units' (case-insensitive suffix)
_AMT_RE = re.compile(r"^([\d,]+(?:\.\d+)?)([A-Za-z]+)$")


def _parse_amount_or_units(raw: str) -> tuple[float | None, float | None]:
    """Return (amount, units). Exactly one is non-None for valid input."""
    m = _AMT_RE.match(raw.strip())
    if not m:
        return None, None
    value = float(m.group(1).replace(",", ""))
    suffix = m.group(2).upper()
    if suffix == "UNITS":
        return None, value
    # otherwise the suffix is a currency code (USD, SGD, ...)
    return value, None


def _order_key(fund_name: str, order_time: datetime, order_type: str, raw_amount: str) -> str:
    """Deterministic natural key — re-running ingest with same rows is a no-op."""
    raw = f"{fund_name}|{order_time.isoformat()}|{order_type}|{raw_amount}"
    return hashlib.sha256(raw.encode()).hexdigest()


def ingest_fund_csv(csv_path: Path) -> dict:
    csv_path = Path(csv_path)
    create_all()
    inserted = 0
    skipped_existing = 0
    skipped_invalid = 0

    with open(csv_path, encoding="utf-8-sig", newline="") as f, Session() as session:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                order_type = row["Order type"].strip()
                status = row["Status"].strip()
                fund_name = row["Product Name"].strip()
                raw_amount = row["Amount/Units"].strip()
                ccy = row["Currency"].strip()
                order_time = datetime.strptime(row["Order Time"].strip(),
                                                "%Y/%m/%d %H:%M:%S")
            except (KeyError, ValueError):
                skipped_invalid += 1
                continue

            amount, units = _parse_amount_or_units(raw_amount)
            if amount is None and units is None:
                skipped_invalid += 1
                continue

            tk = _order_key(fund_name, order_time, order_type, raw_amount)
            if session.query(FundOrder).filter_by(order_key=tk).one_or_none():
                skipped_existing += 1
                continue

            session.add(FundOrder(
                order_key=tk, fund_name=fund_name, order_type=order_type,
                status=status, amount=amount, units=units, ccy=ccy,
                order_time=order_time,
            ))
            inserted += 1
        session.commit()

    return {
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "skipped_invalid": skipped_invalid,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 -m src.fund_csv <path/to/fund accounts.csv>")
        sys.exit(1)
    print(ingest_fund_csv(Path(sys.argv[1])))
