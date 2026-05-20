"""
SQLite schema for the Moomoo dashboard.

We use one SQLite file: data/portfolio.db.
Re-running create_all() is safe; SQLAlchemy only creates missing tables.

Design notes
------------
* `statement_files` is the registry of every PDF we've ingested. The sha256
  + filename combination makes ingestion idempotent — dropping the same PDF
  in again is a no-op.
* `cash_events` is the canonical capital-flow ledger. Every row is traced
  back to its source PDF via source_file_id, and de-duped via source_row_hash.
* `nav_snapshots` stores month-end (and month-start) cash balances per
  currency, plus the FX rate Moomoo declared at that snapshot date. This is
  our benchmark FX series (statement-derived).
* `positions_snapshots` stores month-end holdings per security with both
  trade-ccy and SGD-converted market values.

To inspect the DB outside Python:
    sqlite3 data/portfolio.db
    .tables
    .schema cash_events
"""

import os
from pathlib import Path
from sqlalchemy import (
    create_engine, Column, Integer, Float, String, Date, DateTime, ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Allow override via env var so Streamlit Cloud can point at a bundled demo DB
# without changing code.
_default_db = str(PROJECT_ROOT / "data" / "portfolio.db")
DB_PATH = Path(os.environ.get("PORTFOLIO_DB_PATH", _default_db))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", future=True)
Session = sessionmaker(bind=engine, future=True)
Base = declarative_base()


class StatementFile(Base):
    __tablename__ = "statement_files"
    id = Column(Integer, primary_key=True)
    filename = Column(String, nullable=False)
    sha256 = Column(String, unique=True, nullable=False)
    period_start = Column(Date)
    period_end = Column(Date)
    ingested_at = Column(DateTime)
    parser_version = Column(String)


class NavSnapshot(Base):
    __tablename__ = "nav_snapshots"
    id = Column(Integer, primary_key=True)
    snapshot_date = Column(Date, nullable=False)
    ccy = Column(String, nullable=False)
    cash_balance = Column(Float)
    fx_rate_to_sgd = Column(Float)
    source_file_id = Column(Integer, ForeignKey("statement_files.id"))
    __table_args__ = (UniqueConstraint("snapshot_date", "ccy",
                                       name="uq_navsnap_date_ccy"),)


class CashEvent(Base):
    __tablename__ = "cash_events"
    id = Column(Integer, primary_key=True)
    event_time = Column(DateTime, nullable=False)
    ccy = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    amount = Column(Float, nullable=False)     # positive = in, negative = out
    comment = Column(String)
    fx_rate = Column(Float)                    # if Currency Exchange, parsed from comment
    source_file_id = Column(Integer, ForeignKey("statement_files.id"))
    source_row_hash = Column(String, unique=True, nullable=False)


class PositionSnapshot(Base):
    __tablename__ = "positions_snapshots"
    id = Column(Integer, primary_key=True)
    snapshot_date = Column(Date, nullable=False)
    symbol = Column(String, nullable=False)
    exchange = Column(String)
    ccy = Column(String)
    settled_qty = Column(Float)
    unsettled_qty = Column(Float)
    closing_price = Column(Float)
    market_value = Column(Float)               # in trade currency
    fx_rate_to_sgd = Column(Float)
    market_value_sgd = Column(Float)
    source_file_id = Column(Integer, ForeignKey("statement_files.id"))
    __table_args__ = (UniqueConstraint("snapshot_date", "symbol",
                                       name="uq_pos_date_symbol"),)


# ---------------------------------------------------------------------------
# Phase 2 tables
# ---------------------------------------------------------------------------

class FxRate(Base):
    """Daily FX rates pulled from external sources. We always store rate-to-SGD
    (i.e. how many SGD = 1 unit of `ccy`)."""
    __tablename__ = "fx_rates"
    id = Column(Integer, primary_key=True)
    rate_date = Column(Date, nullable=False)
    ccy = Column(String, nullable=False)
    rate_to_sgd = Column(Float, nullable=False)
    source = Column(String)
    __table_args__ = (UniqueConstraint("rate_date", "ccy", "source",
                                       name="uq_fx_date_ccy_source"),)


class Trade(Base):
    """Parsed from History_xx.xx.xx.csv. One row per fill (after multi-fill
    rollup)."""
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    trade_key = Column(String, unique=True, nullable=False)  # natural key
    fill_time = Column(DateTime, nullable=False)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)        # Buy / Sell / Short Sell
    qty = Column(Float)
    fill_price = Column(Float)
    fill_amount = Column(Float)
    fees_total = Column(Float)
    net_cash_impact = Column(Float)              # signed: -ve on buy, +ve on sell
    ccy = Column(String)
    market = Column(String)


class LiveCashSnapshot(Base):
    """Point-in-time snapshot of cash balances from OpenAPI."""
    __tablename__ = "live_cash_snapshots"
    id = Column(Integer, primary_key=True)
    snapshot_time = Column(DateTime, nullable=False)
    ccy = Column(String, nullable=False)
    cash = Column(Float)
    total_assets_sgd = Column(Float)             # only set on the SGD row


class LivePositionSnapshot(Base):
    __tablename__ = "live_position_snapshots"
    id = Column(Integer, primary_key=True)
    snapshot_time = Column(DateTime, nullable=False)
    symbol = Column(String, nullable=False)
    qty = Column(Float)
    cost_price = Column(Float)
    nominal_price = Column(Float)
    market_value = Column(Float)
    pl_val = Column(Float)
    ccy = Column(String)


class FundOrder(Base):
    """One row per fund subscription/redemption from the Moomoo fund-orders CSV.
    Funds appear separately from equity trades — they have their own order types
    (Subscribe/Redeem), can be specified in either amount OR units, and have
    different statuses (Completed/Submitted/Terminated). Money impact is also
    captured in cash_events from statements; this table is for audit trail and
    fund-level P&L aggregation."""
    __tablename__ = "fund_orders"
    id = Column(Integer, primary_key=True)
    order_key = Column(String, unique=True, nullable=False)   # natural key (sha256)
    fund_name = Column(String, nullable=False)
    order_type = Column(String, nullable=False)               # 'Subscribe' or 'Redeem'
    status = Column(String, nullable=False)                   # Completed / Submitted / Terminated
    amount = Column(Float)                                    # None if order was units-based
    units = Column(Float)                                     # None if order was amount-based
    ccy = Column(String, nullable=False)
    order_time = Column(DateTime, nullable=False)


class ManualCashEvent(Base):
    """User-logged deposits/withdrawals that haven't been confirmed by a
    statement yet. When the next statement arrives, matching events are
    marked `confirmed_at`; unmatched ones remain pending and surface in the
    reconciliation report."""
    __tablename__ = "manual_cash_events"
    id = Column(Integer, primary_key=True)
    event_date = Column(Date, nullable=False)
    ccy = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    memo = Column(String)
    logged_at = Column(DateTime)
    confirmed_at = Column(DateTime)


class ExternalCashBalance(Base):
    """Cash held outside the Moomoo trading-account API view. Examples:
       - SGD sitting in your Moomoo Cash sub-account (not the Margin account
         that accinfo_query pulls)
       - Cash at another broker or in a bank account you consider part of the
         portfolio for P&L tracking purposes
    The LATEST entry per (ccy) is the current balance; older ones are history.
    """
    __tablename__ = "external_cash_balances"
    id = Column(Integer, primary_key=True)
    ccy = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    as_of_date = Column(Date, nullable=False)
    source = Column(String)                # e.g. "Moomoo Cash sub-account", "DBS savings"
    memo = Column(String)
    logged_at = Column(DateTime)


class TargetWeight(Base):
    """User-specified target portfolio weight for a symbol, in percent.
       e.g. (RKLB, 10.0) = you want RKLB to be 10% of total portfolio.
    """
    __tablename__ = "target_weights"
    id = Column(Integer, primary_key=True)
    symbol = Column(String, unique=True, nullable=False)
    target_weight_pct = Column(Float, nullable=False)
    notes = Column(String)


def create_all():
    Base.metadata.create_all(engine)


if __name__ == "__main__":
    create_all()
    print(f"DB ready at {DB_PATH}")
