"""
FX rate backfill from yfinance.

We store rate-to-SGD per currency per date. Sources we use:
    USD/SGD -> ticker "SGD=X" (USD per 1 SGD)
        => rate_to_sgd_for_USD = 1 / SGD=X close
    HKD/SGD -> ticker "SGDHKD=X" (HKD per 1 SGD)
        => rate_to_sgd_for_HKD = 1 / SGDHKD=X close
    JPY/SGD -> ticker "SGDJPY=X"
        => rate_to_sgd_for_JPY = 1 / SGDJPY=X close

Idempotent: re-running only inserts new (date, ccy) rows.

Usage:
    python3 -m src.fx                 # backfill from earliest statement onwards
    python3 -m src.fx 2024-11-01      # backfill from this date
"""

from __future__ import annotations
import sys
from datetime import date, timedelta
from pathlib import Path

import yfinance as yf

from src.db import Session, create_all, FxRate, NavSnapshot


# For each currency we store rate_to_sgd, meaning "multiply this many units of
# foreign ccy by rate_to_sgd to get SGD". yfinance ticker conventions differ:
#
#   SGD=X     == USDSGD   -> price = SGD per 1 USD       -> use DIRECTLY for USD
#   SGDHKD=X  == SGDHKD   -> price = HKD per 1 SGD       -> INVERT for HKD
#   SGDJPY=X  == SGDJPY   -> price = JPY per 1 SGD       -> INVERT for JPY
#
# The third item in the tuple is True if we need to invert (1/price), False if
# the price is already "SGD per 1 foreign-ccy-unit".
_CCY_TICKERS = {
    "USD": ("SGD=X",    False),
    "HKD": ("SGDHKD=X", True),
    "JPY": ("SGDJPY=X", True),
}


def _earliest_statement_date(session) -> date:
    row = session.query(NavSnapshot).order_by(NavSnapshot.snapshot_date.asc()).first()
    if row is None:
        return date.today() - timedelta(days=400)
    return row.snapshot_date


def backfill(start: date | None = None) -> dict:
    create_all()
    with Session() as session:
        if start is None:
            start = _earliest_statement_date(session)
        end = date.today() + timedelta(days=1)
        inserted = 0

        for ccy, (ticker, invert) in _CCY_TICKERS.items():
            print(f"  fetching {ticker} from {start} to {end} (invert={invert})...")
            hist = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
            if hist is None or hist.empty:
                print(f"    no data for {ticker}")
                continue
            for ts, row in hist.iterrows():
                rate = float(row["Close"].iloc[0] if hasattr(row["Close"], "iloc") else row["Close"])
                if rate <= 0:
                    continue
                rate_to_sgd = (1.0 / rate) if invert else rate
                d = ts.date() if hasattr(ts, "date") else ts
                # Overwrite rather than skip — old rows may have the bug
                existing = session.query(FxRate).filter_by(
                    rate_date=d, ccy=ccy, source="yfinance"
                ).one_or_none()
                if existing:
                    existing.rate_to_sgd = rate_to_sgd
                    continue
                session.add(FxRate(
                    rate_date=d, ccy=ccy, rate_to_sgd=rate_to_sgd, source="yfinance",
                ))
                inserted += 1
            # SGD itself
        # SGD rate is always 1.0
        existing = session.query(FxRate).filter_by(ccy="SGD").first()
        if not existing:
            d = start
            while d <= date.today():
                session.add(FxRate(rate_date=d, ccy="SGD", rate_to_sgd=1.0, source="constant"))
                d += timedelta(days=1)
                inserted += 1
        session.commit()
    return {"inserted": inserted, "start": str(start), "end": str(end)}


if __name__ == "__main__":
    start = None
    if len(sys.argv) > 1:
        from datetime import datetime
        start = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    print(backfill(start))
