"""
Parse the Moomoo History CSV into the trades table.

The CSV has a quirk: multi-fill orders span multiple rows, where the first
row has the order info and subsequent rows have only the fill detail (Side,
Symbol, Name etc. blank). We roll those continuations up into the parent.

Usage:
    python3 -m src.csv_trades /path/to/History_xx.xx.xx.csv
or:
    from src.csv_trades import ingest_csv
"""

from __future__ import annotations
import csv
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

from dateutil import parser as dateparser

from src.db import Session, create_all, Trade


# Map abbreviated tz codes in the CSV to ones dateutil understands.
_TZINFOS = {
    "ET":  -4 * 3600,   # Eastern (handles DST loosely; we trade-date only)
    "EDT": -4 * 3600,
    "EST": -5 * 3600,
    "SGT":  8 * 3600,
    "HKT":  8 * 3600,
    "JST":  9 * 3600,
}


def _parse_when(s: str) -> datetime | None:
    if not s.strip():
        return None
    try:
        return dateparser.parse(s, tzinfos=_TZINFOS)
    except Exception:
        return None


def _to_float(s: str) -> float:
    s = (s or "").strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _trade_key(side: str, symbol: str, fill_time: datetime, qty: float, price: float) -> str:
    raw = f"{side}|{symbol}|{fill_time.isoformat()}|{qty:.6f}|{price:.6f}"
    return hashlib.sha256(raw.encode()).hexdigest()


# CSV has two columns each named "Markets" and "Currency" — one for the order,
# one for the fill. csv.DictReader keeps only the last value, but we want the
# FILL one (which is what actually settled). The fill column is index 22/23.
def _fill_market_and_ccy(raw_row: list[str]) -> tuple[str, str]:
    # Columns (0-indexed) from inspection: 22 = fill Markets, 23 = fill Currency
    market = raw_row[22] if len(raw_row) > 22 else ""
    ccy = raw_row[23] if len(raw_row) > 23 else ""
    return market.strip(), ccy.strip()


def ingest_csv(csv_path: Path) -> dict:
    csv_path = Path(csv_path)
    create_all()

    n_inserted = 0
    n_skipped_existing = 0
    n_skipped_not_filled = 0

    with open(csv_path, encoding="utf-8-sig", newline="") as f, Session() as session:
        reader = csv.reader(f)
        header = next(reader)
        # Build index map from header for our named fields
        idx = {name: i for i, name in enumerate(header)}

        current_parent = None    # last seen "real" order row context

        for raw in reader:
            # Pad to header length so indexing is safe
            while len(raw) < len(header):
                raw.append("")

            side = raw[idx["Side"]].strip()
            symbol = raw[idx["Symbol"]].strip()
            status = raw[idx["Status"]].strip()

            if side and symbol:
                # New order header row
                current_parent = {
                    "side": side, "symbol": symbol,
                    "name": raw[idx["Name"]].strip(),
                }

            # Whether this row carries a fill: Fill Qty / Fill Price / Fill Amount populated
            fill_qty = _to_float(raw[idx["Fill Qty"]])
            fill_price = _to_float(raw[idx["Fill Price"]])
            fill_amount = _to_float(raw[idx["Fill Amount"]])
            fill_time = _parse_when(raw[idx["Fill Time"]])

            if not fill_time or fill_qty == 0:
                # No fill on this row.
                if side and symbol and status not in ("Filled", "Partially Filled"):
                    n_skipped_not_filled += 1
                continue

            if not current_parent:
                continue

            market, ccy = _fill_market_and_ccy(raw)

            # Total fee = sum of all fee fields on the row
            fee_cols = [
                "Platform Fees", "Settlement Fees", "Consumption Tax",
                "SEC Fees", "Trading Activity Fees", "Commission",
                "Trading Fees", "Clearing Fees", "Consolidated Audit Trail Fees",
                "Trading Tariff", "Stamp Duty", "SFC Levy", "FRC Levy",
                "Platform Fee",
            ]
            fees_total = sum(_to_float(raw[idx[c]]) for c in fee_cols if c in idx)

            net = -fill_amount - fees_total if current_parent["side"] == "Buy" else fill_amount - fees_total

            tk = _trade_key(current_parent["side"], current_parent["symbol"],
                            fill_time, fill_qty, fill_price)
            existing = session.query(Trade).filter_by(trade_key=tk).one_or_none()
            if existing:
                n_skipped_existing += 1
                continue
            session.add(Trade(
                trade_key=tk, fill_time=fill_time,
                symbol=current_parent["symbol"], side=current_parent["side"],
                qty=fill_qty, fill_price=fill_price, fill_amount=fill_amount,
                fees_total=fees_total, net_cash_impact=net,
                ccy=ccy, market=market,
            ))
            n_inserted += 1
        session.commit()

    return {
        "inserted": n_inserted,
        "skipped_existing": n_skipped_existing,
        "skipped_not_filled": n_skipped_not_filled,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 -m src.csv_trades <path/to/History.csv>")
        sys.exit(1)
    res = ingest_csv(Path(sys.argv[1]))
    print(res)
