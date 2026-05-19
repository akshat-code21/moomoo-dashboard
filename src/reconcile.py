"""
Reconciliation: what's the gap between the ledger and live OpenAPI cash?

Method:
  1. Pick the most recent statement (last `period_end`).
  2. Look up the cash balance per currency at that date (from nav_snapshots).
  3. Walk forward from that date through:
        + filled trades (from `trades`, signed via net_cash_impact)
        + manual_cash_events not yet confirmed
        + dividends/etc. recorded after the statement (rare; usually 0)
  4. Compute expected current cash per ccy.
  5. Compare to the latest live_cash_snapshots row per ccy.
  6. Drift = live - expected. Drift > 1.00 -> probable unrecorded deposit.

Usage:
    python3 -m src.reconcile
"""

from __future__ import annotations
from datetime import datetime
from pathlib import Path
import sys

from sqlalchemy import func

from src.db import (
    Session, create_all,
    StatementFile, NavSnapshot, CashEvent, PositionSnapshot,
    Trade, LiveCashSnapshot, LivePositionSnapshot, ManualCashEvent,
    FxRate,
)

TOLERANCE = 5.00   # SGD; small drift tolerated for FX/rounding noise


def compute_drift_summary(session) -> dict:
    """Headless version of reconcile() that returns a summary dict instead of printing.
    Used by run_phase2.py + notify.py to decide whether to alert.
    """
    latest_stmt = session.query(StatementFile).order_by(StatementFile.period_end.desc()).first()
    if not latest_stmt:
        return {"status": "no_statements", "max_abs_drift_sgd": 0.0, "details": {}}
    cutoff = latest_stmt.period_end

    nav_rows = session.query(NavSnapshot).filter(NavSnapshot.snapshot_date == cutoff).all()
    expected = {r.ccy: (r.cash_balance or 0.0) for r in nav_rows}

    trades = session.query(Trade).filter(
        Trade.fill_time > datetime.combine(cutoff, datetime.min.time())
    ).all()
    for t in trades:
        expected[t.ccy] = expected.get(t.ccy, 0.0) + (t.net_cash_impact or 0.0)

    manuals = session.query(ManualCashEvent).filter(
        ManualCashEvent.event_date > cutoff,
        ManualCashEvent.confirmed_at.is_(None),
    ).all()
    for m in manuals:
        expected[m.ccy] = expected.get(m.ccy, 0.0) + (m.amount or 0.0)

    latest_snap_time = session.query(func.max(LiveCashSnapshot.snapshot_time)).scalar()
    if not latest_snap_time:
        return {"status": "no_live_snapshot", "max_abs_drift_sgd": 0.0, "details": {}}
    live_rows = session.query(LiveCashSnapshot).filter(
        LiveCashSnapshot.snapshot_time == latest_snap_time
    ).all()
    live = {r.ccy: (r.cash or 0.0) for r in live_rows}

    # Per-currency drift, converted to SGD via latest FX
    details = {}
    max_abs_drift = 0.0
    for ccy in set(list(expected) + list(live)):
        e = expected.get(ccy, 0.0)
        l = live.get(ccy, 0.0)
        d = l - e
        rate = _latest_fx_rate(session, ccy)
        d_sgd = d * rate
        details[ccy] = {"expected": e, "live": l, "drift": d, "drift_sgd": d_sgd}
        if abs(d_sgd) > max_abs_drift:
            max_abs_drift = abs(d_sgd)
    return {"status": "ok", "max_abs_drift_sgd": max_abs_drift, "details": details}


def _latest_fx_rate(session, ccy: str) -> float:
    """Return the most recent FX rate-to-SGD for ccy. Fallback to 1.0 if missing."""
    if ccy.upper() == "SGD":
        return 1.0
    row = (session.query(FxRate)
           .filter(FxRate.ccy == ccy.upper())
           .order_by(FxRate.rate_date.desc())
           .first())
    return row.rate_to_sgd if row else 1.0


def reconcile() -> None:
    create_all()
    with Session() as session:
        # 1. Most recent statement
        latest_stmt = session.query(StatementFile).order_by(StatementFile.period_end.desc()).first()
        if not latest_stmt:
            print("No statements ingested yet. Run run_ingest.py first.")
            return
        cutoff = latest_stmt.period_end
        print(f"Latest statement: {latest_stmt.filename}  period_end={cutoff}")

        # 2. Ending cash per ccy at cutoff
        nav_rows = session.query(NavSnapshot).filter(NavSnapshot.snapshot_date == cutoff).all()
        ending = {r.ccy: r.cash_balance for r in nav_rows}
        print(f"\nEnding cash at {cutoff} (statement-derived):")
        for ccy, c in sorted(ending.items()):
            if c:
                print(f"  {ccy:5s}  {c:>14,.2f}")

        # 3. Walk forward
        expected = dict(ending)
        # 3a. Trades after cutoff (CSV-derived)
        trades = session.query(Trade).filter(Trade.fill_time > datetime.combine(cutoff, datetime.min.time())).all()
        trade_impact = {}
        for t in trades:
            trade_impact[t.ccy] = trade_impact.get(t.ccy, 0.0) + t.net_cash_impact
        for ccy, v in trade_impact.items():
            expected[ccy] = expected.get(ccy, 0.0) + v

        # 3b. Manual cash events between cutoff and now
        manuals = session.query(ManualCashEvent).filter(
            ManualCashEvent.event_date > cutoff,
            ManualCashEvent.confirmed_at.is_(None),
        ).all()
        manual_impact = {}
        for m in manuals:
            manual_impact[m.ccy] = manual_impact.get(m.ccy, 0.0) + m.amount
            expected[m.ccy] = expected.get(m.ccy, 0.0) + m.amount

        # 4. Latest live snapshot per ccy
        latest_snap_time = session.query(func.max(LiveCashSnapshot.snapshot_time)).scalar()
        if not latest_snap_time:
            print("\nNo OpenAPI snapshot yet. Run openapi_sync.py first.")
            return
        live_rows = session.query(LiveCashSnapshot).filter(
            LiveCashSnapshot.snapshot_time == latest_snap_time
        ).all()
        live = {r.ccy: r.cash for r in live_rows}

        print(f"\nLive snapshot at {latest_snap_time}:")
        for ccy, c in sorted(live.items()):
            if c:
                print(f"  {ccy:5s}  {c:>14,.2f}")

        # Trade impact summary + fill-by-fill detail per currency
        if trade_impact:
            print(f"\nTrades since {cutoff}: {len(trades)} fills, net cash impact:")
            for ccy, v in sorted(trade_impact.items()):
                print(f"  {ccy:5s}  {v:>+14,.2f}")
            # Detail per fill, grouped by ccy
            from collections import defaultdict
            by_ccy = defaultdict(list)
            for t in trades:
                by_ccy[t.ccy].append(t)
            print(f"\nTrade detail (cross-reference against your CSV):")
            for ccy in sorted(by_ccy):
                print(f"\n  --- {ccy} ---")
                print(f"  {'date':10s}  {'side':4s} {'symbol':8s} {'qty':>8s} {'price':>10s} {'fees':>7s} {'net':>11s}")
                running = 0.0
                for t in sorted(by_ccy[ccy], key=lambda x: x.fill_time):
                    running += t.net_cash_impact
                    print(f"  {t.fill_time.strftime('%Y-%m-%d'):10s}  {t.side:4s} {t.symbol:8s} {t.qty:>8.2f} {t.fill_price:>10.4f} {t.fees_total:>7.2f} {t.net_cash_impact:>+11.2f}")
                print(f"  {' ':10s}  {' ':4s} {' ':8s} {' ':>8s} {' ':>10s} {'sum:':>7s} {running:>+11.2f}")
        if manual_impact:
            print(f"\nManual cash events since {cutoff}:")
            for ccy, v in sorted(manual_impact.items()):
                print(f"  {ccy:5s}  {v:>+14,.2f}")

        # 5. Drift
        # Detect mode: per-currency live rows vs single SGD-aggregated row.
        live_ccys = set(live)
        if live_ccys == {"SGD"} and len(expected) > 1:
            # SG account: live API returned a single SGD-aggregated cash row.
            # Convert expected (per-ccy) to SGD using latest FX, then compare.
            expected_sgd = 0.0
            print("\nExpected cash converted to SGD (latest FX):")
            print(f"  {'ccy':5s}  {'cash':>14s}  {'fx_to_sgd':>10s}  {'sgd_equiv':>14s}")
            for ccy, c in sorted(expected.items()):
                if c == 0:
                    continue
                fx = _latest_fx_rate(session, ccy)
                sgd = c * fx
                expected_sgd += sgd
                print(f"  {ccy:5s}  {c:>14,.2f}  {fx:>10.6f}  {sgd:>14,.2f}")
            live_sgd = live.get("SGD", 0.0)
            drift = live_sgd - expected_sgd
            status = "OK" if abs(drift) <= TOLERANCE else (
                "DEPOSIT?" if drift > 0 else "WITHDRAWAL?"
            )
            print(f"\n  SGD-equivalent expected cash:  {expected_sgd:>14,.2f}")
            print(f"  SGD-equivalent live total:     {live_sgd:>14,.2f}")
            print(f"  Drift (live - expected):       {drift:>+14,.2f}   {status}")
            any_drift = status != "OK"
        else:
            all_ccys = set(expected) | set(live)
            print("\nReconciliation:")
            print(f"  {'ccy':5s}  {'expected':>14s}  {'live':>14s}  {'drift':>14s}  status")
            any_drift = False
            for ccy in sorted(all_ccys):
                e = expected.get(ccy, 0.0)
                l = live.get(ccy, 0.0)
                d = l - e
                status = "OK" if abs(d) <= TOLERANCE else (
                    "DEPOSIT?" if d > 0 else "WITHDRAWAL?"
                )
                print(f"  {ccy:5s}  {e:>14,.2f}  {l:>14,.2f}  {d:>+14,.2f}  {status}")
                if status != "OK":
                    any_drift = True

        if any_drift:
            print("\nNon-zero drift detected. If you deposited or withdrew cash since")
            print(f"{cutoff} and it isn't yet in a statement, log it:")
            print("  python3 -m src.reconcile add-flow YYYY-MM-DD CCY AMOUNT 'memo'")
        else:
            print("\nLedger and live snapshot agree within tolerance.")


def add_flow(args: list[str]) -> None:
    """Log a one-sided deposit or withdrawal (SGD only, in practice)."""
    if len(args) < 3:
        print("Usage: python3 -m src.reconcile add-flow YYYY-MM-DD CCY AMOUNT [memo]")
        sys.exit(1)
    from datetime import date as _date
    d = _date.fromisoformat(args[0])
    ccy = args[1].upper()
    amt = float(args[2])
    memo = " ".join(args[3:]) if len(args) > 3 else ""
    create_all()
    with Session() as session:
        session.add(ManualCashEvent(
            event_date=d, ccy=ccy, amount=amt, memo=memo,
            logged_at=datetime.utcnow(),
        ))
        session.commit()
    print(f"Logged: {d} {ccy} {amt:+.2f} '{memo}'")


def add_fx(args: list[str]) -> None:
    """Log a currency exchange (two legs in one call).
       Usage: add-fx YYYY-MM-DD FROM_CCY FROM_AMT TO_CCY TO_AMT [memo]
       Example: add-fx 2026-05-08 USD 631 SGD 801 'Moomoo FX'
       This records: -631 USD and +801 SGD on 2026-05-08.
    """
    if len(args) < 5:
        print("Usage: python3 -m src.reconcile add-fx "
              "YYYY-MM-DD FROM_CCY FROM_AMT TO_CCY TO_AMT [memo]")
        print("Example: add-fx 2026-05-08 USD 631 SGD 801 'Moomoo FX'")
        sys.exit(1)
    from datetime import date as _date
    d = _date.fromisoformat(args[0])
    from_ccy = args[1].upper()
    from_amt = abs(float(args[2]))    # always treat as positive input
    to_ccy = args[3].upper()
    to_amt = abs(float(args[4]))
    memo = " ".join(args[5:]) if len(args) > 5 else f"FX {from_ccy}->{to_ccy}"
    create_all()
    with Session() as session:
        session.add(ManualCashEvent(
            event_date=d, ccy=from_ccy, amount=-from_amt,
            memo=f"{memo} (out)", logged_at=datetime.utcnow(),
        ))
        session.add(ManualCashEvent(
            event_date=d, ccy=to_ccy, amount=+to_amt,
            memo=f"{memo} (in, rate={to_amt/from_amt:.6f})",
            logged_at=datetime.utcnow(),
        ))
        session.commit()
    implied_rate = to_amt / from_amt
    print(f"Logged FX: {d}  {from_ccy} {-from_amt:+,.2f}  ->  {to_ccy} {to_amt:+,.2f}  "
          f"(rate {implied_rate:.6f})")


def list_manual(args: list[str]) -> None:
    """Show all manual cash events you've logged."""
    create_all()
    with Session() as session:
        rows = session.query(ManualCashEvent).order_by(ManualCashEvent.event_date).all()
        if not rows:
            print("No manual events logged.")
            return
        print(f"  {'date':10s}  {'ccy':5s}  {'amount':>12s}  status   memo")
        for r in rows:
            status = "confirmed" if r.confirmed_at else "pending"
            print(f"  {r.event_date}  {r.ccy:5s}  {r.amount:>+12,.2f}  {status:9s} {r.memo}")


def delete_manual(args: list[str]) -> None:
    """Delete a manual event by id (use 'list' to see ids first)."""
    if not args:
        print("Usage: python3 -m src.reconcile delete-manual <id>")
        sys.exit(1)
    create_all()
    with Session() as session:
        row = session.query(ManualCashEvent).filter_by(id=int(args[0])).one_or_none()
        if not row:
            print("Not found.")
            return
        session.delete(row)
        session.commit()
    print(f"Deleted manual event {args[0]}.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "add-flow":
        add_flow(sys.argv[2:])
    elif cmd == "add-fx":
        add_fx(sys.argv[2:])
    elif cmd == "list":
        list_manual(sys.argv[2:])
    elif cmd == "delete-manual":
        delete_manual(sys.argv[2:])
    else:
        reconcile()
