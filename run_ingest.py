"""
Top-level ETL runner.

Usage:  python3 run_ingest.py

Walks statements/raw/, parses every PDF, writes new rows to data/portfolio.db.
Idempotent: re-running is a no-op for files already ingested (matched by sha256).
"""

from datetime import datetime, timezone
from pathlib import Path
import sys

# Make src/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.db import (
    Session, create_all,
    StatementFile, CashEvent, NavSnapshot, PositionSnapshot,
)
from src.parse_statement import parse_statement, PARSER_VERSION, ReconciliationError

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "statements" / "raw"


def ingest_one(pdf_path: Path, session) -> str:
    """Parse one PDF and write to DB. Returns a one-line status string."""
    # Check if already ingested
    parsed = parse_statement(pdf_path)
    existing = session.query(StatementFile).filter_by(sha256=parsed.sha256).one_or_none()
    if existing:
        return f"  skip  {pdf_path.name}  (already ingested as file_id={existing.id})"

    sf = StatementFile(
        filename=pdf_path.name,
        sha256=parsed.sha256,
        period_start=parsed.period_start,
        period_end=parsed.period_end,
        ingested_at=datetime.now(timezone.utc),
        parser_version=PARSER_VERSION,
    )
    session.add(sf)
    session.flush()  # get sf.id

    # NAV snapshots — start and end.
    # Prefer the ACTUAL ending cash from the cash-ledger section if the
    # parser found one; fall back to the per-ccy NAV (older months may not
    # have ledger entries when nothing happened).
    for snap_date, ledger_cash, nav, fx in [
        (parsed.period_start, parsed.starting_cash, parsed.nav_start, parsed.fx_start),
        (parsed.period_end,   parsed.ending_cash,   parsed.nav_end,   parsed.fx_end),
    ]:
        for ccy in set(list(ledger_cash) + list(nav)):
            cash = ledger_cash.get(ccy, nav.get(ccy, 0.0))
            session.merge(NavSnapshot(
                snapshot_date=snap_date, ccy=ccy,
                cash_balance=cash,
                fx_rate_to_sgd=fx.get(ccy),
                source_file_id=sf.id,
            ))

    # Cash events
    n_events = 0
    for e in parsed.cash_events:
        # de-dup across re-runs
        existing_e = session.query(CashEvent).filter_by(source_row_hash=e.row_hash).one_or_none()
        if existing_e:
            continue
        session.add(CashEvent(
            event_time=e.event_time, ccy=e.ccy, event_type=e.event_type,
            amount=e.amount, comment=e.comment, fx_rate=e.fx_rate,
            source_file_id=sf.id, source_row_hash=e.row_hash,
        ))
        n_events += 1

    # Positions
    for p in parsed.positions:
        session.merge(PositionSnapshot(
            snapshot_date=p.snapshot_date,
            symbol=p.symbol, exchange=p.exchange, ccy=p.ccy,
            settled_qty=p.settled_qty, unsettled_qty=p.unsettled_qty,
            closing_price=p.closing_price,
            market_value=p.market_value,
            fx_rate_to_sgd=p.fx_rate_to_sgd,
            market_value_sgd=p.market_value_sgd,
            source_file_id=sf.id,
        ))

    return (f"  OK    {pdf_path.name}  period={parsed.period_start}..{parsed.period_end}"
            f"  events={n_events}  positions={len(parsed.positions)}")


def main():
    create_all()
    pdfs = sorted(RAW_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {RAW_DIR}")
        return

    print(f"Ingesting {len(pdfs)} PDF(s) from {RAW_DIR}")
    # CRITICAL: one session per file, so a single bad statement doesn't
    # roll back the rest of the batch.
    n_failed = 0
    for pdf in pdfs:
        with Session() as session:
            try:
                status = ingest_one(pdf, session)
                session.commit()
                print(status)
            except ReconciliationError as e:
                session.rollback()
                n_failed += 1
                print(f"  FAIL  {pdf.name}\n{e}")
            except Exception as e:
                session.rollback()
                n_failed += 1
                print(f"  ERROR {pdf.name}: {type(e).__name__}: {e}")
    if n_failed:
        print(f"\n{n_failed} file(s) failed — fix and re-run; idempotent.")

    # Print a quick summary of what's in the DB now
    with Session() as session:
        n_files = session.query(StatementFile).count()
        n_events = session.query(CashEvent).count()
        n_flows = session.query(CashEvent).filter(CashEvent.event_type == "Cash In Out").count()
        n_pos = session.query(PositionSnapshot).count()
        print(f"\nDB state: {n_files} statements, {n_events} cash events, "
              f"{n_flows} capital in/out events, {n_pos} positions")


if __name__ == "__main__":
    main()
