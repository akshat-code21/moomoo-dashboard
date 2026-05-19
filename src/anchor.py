"""
Anchor-and-delta state model.

Mental model:
  1. The LATEST monthly statement is the SOURCE OF TRUTH ("anchor"). Everything
     in it is reconciled by Moomoo and exactly matches what they recorded —
     cash per currency, positions, fund holdings, cumulative deposits, etc.
  2. Since the anchor date, three things may have happened:
        - Equity trades (from History*.csv)
        - Fund subscriptions/redemptions (from fund_orders CSV)
        - Manual cash events (deposits, withdrawals, FX conversions)
  3. PROJECTED CURRENT STATE = anchor + delta.
  4. RECONCILIATION = (live API state) vs (anchor + delta).

This is more reliable than reading live data alone because:
  - The statement is authoritative
  - Only PRICES need to be fetched live (for mark-to-market)
  - Drift is clearly attributable to either:
       (a) a missed manual event, or
       (b) an event Moomoo recorded but we haven't ingested
"""

from __future__ import annotations
from datetime import date, datetime
from typing import Optional

import pandas as pd
from sqlalchemy import func

from src.db import (
    Session, StatementFile, NavSnapshot, PositionSnapshot, CashEvent,
    ManualCashEvent, LiveCashSnapshot, LivePositionSnapshot, Trade, FundOrder,
)
from src.stats import fx_at


def _is_fund_symbol(symbol: str, exchange: str) -> bool:
    if exchange in ("FD", "--", "", None):
        return True
    return bool(symbol) and symbol.startswith(("SG9999", "SGXZ", "LU", "IE", "BMG"))


# ---------------------------------------------------------------------------
# Anchor: the most recent statement state
# ---------------------------------------------------------------------------

def anchor_state(session) -> dict:
    """Snapshot of everything as of the latest statement's period_end."""
    latest = (session.query(StatementFile)
                     .order_by(StatementFile.period_end.desc()).first())
    if not latest:
        return {
            "anchor_date": None, "nav_sgd": 0.0,
            "cash_by_ccy": {}, "positions": [],
            "cumulative_deposits_sgd": 0.0,
        }
    cutoff = latest.period_end

    # Cash per currency (the canonical statement-ending cash, NOT NAV-per-ccy)
    cash_by_ccy: dict[str, dict] = {}
    for nav in session.query(NavSnapshot).filter_by(snapshot_date=cutoff):
        rate = nav.fx_rate_to_sgd or fx_at(session, nav.ccy, cutoff)
        cash = nav.cash_balance or 0.0
        cash_by_ccy[nav.ccy] = {
            "cash": cash, "fx_to_sgd": rate, "cash_sgd": cash * rate,
        }

    # Positions (equities + funds)
    positions = []
    for p in session.query(PositionSnapshot).filter_by(snapshot_date=cutoff):
        positions.append({
            "symbol": p.symbol, "ccy": p.ccy, "exchange": p.exchange,
            "qty": p.settled_qty or 0.0,
            "price": p.closing_price or 0.0,
            "mv_local": p.market_value or 0.0,
            "fx_to_sgd": p.fx_rate_to_sgd or 1.0,
            "mv_sgd": p.market_value_sgd or 0.0,
            "is_fund": _is_fund_symbol(p.symbol, p.exchange),
        })

    cash_sgd_total = sum(c["cash_sgd"] for c in cash_by_ccy.values())
    pos_sgd_total = sum(p["mv_sgd"] for p in positions)
    nav_sgd = cash_sgd_total + pos_sgd_total

    # Cumulative deposits through the anchor (from cash_events + manual events
    # dated on/before the anchor)
    cum_dep = 0.0
    for ev in session.query(CashEvent).filter(CashEvent.event_type == "Cash In Out"):
        if ev.event_time.date() <= cutoff:
            cum_dep += ev.amount * fx_at(session, ev.ccy, ev.event_time)
    for m in session.query(ManualCashEvent):
        if m.event_date <= cutoff:
            cum_dep += m.amount * fx_at(session, m.ccy, m.event_date)

    return {
        "anchor_date": cutoff,
        "anchor_filename": latest.filename,
        "nav_sgd": nav_sgd,
        "cash_sgd_total": cash_sgd_total,
        "positions_sgd_total": pos_sgd_total,
        "cash_by_ccy": cash_by_ccy,
        "positions": positions,
        "cumulative_deposits_sgd": cum_dep,
        "pnl_through_anchor_sgd": nav_sgd - cum_dep,
    }


# ---------------------------------------------------------------------------
# Delta: activity since anchor
# ---------------------------------------------------------------------------

def trades_since_anchor(session, anchor_date: Optional[date]) -> pd.DataFrame:
    if not anchor_date:
        return pd.DataFrame()
    cutoff = datetime.combine(anchor_date, datetime.min.time())
    rows = []
    for t in session.query(Trade).filter(Trade.fill_time > cutoff).order_by(Trade.fill_time):
        rows.append({
            "when": t.fill_time, "symbol": t.symbol, "side": t.side,
            "qty": t.qty, "price": t.fill_price, "fees": t.fees_total,
            "net_cash_impact": t.net_cash_impact, "ccy": t.ccy,
        })
    return pd.DataFrame(rows)


def fund_orders_since_anchor(session, anchor_date: Optional[date]) -> pd.DataFrame:
    if not anchor_date:
        return pd.DataFrame()
    cutoff = datetime.combine(anchor_date, datetime.min.time())
    rows = []
    q = session.query(FundOrder).filter(FundOrder.order_time > cutoff).order_by(FundOrder.order_time)
    for o in q:
        rows.append({
            "when": o.order_time, "fund": o.fund_name, "type": o.order_type,
            "status": o.status,
            "amount": o.amount if o.amount is not None else float("nan"),
            "units": o.units if o.units is not None else float("nan"),
            "ccy": o.ccy,
        })
    return pd.DataFrame(rows)


def manual_events_since_anchor(session, anchor_date: Optional[date]) -> pd.DataFrame:
    if not anchor_date:
        return pd.DataFrame()
    rows = []
    for m in session.query(ManualCashEvent).filter(
        ManualCashEvent.event_date > anchor_date
    ).order_by(ManualCashEvent.event_date):
        rows.append({
            "when": m.event_date, "ccy": m.ccy, "amount": m.amount,
            "memo": m.memo,
        })
    return pd.DataFrame(rows)


def position_qty_deltas(session, anchor_date: Optional[date]) -> dict[str, float]:
    """Per-symbol net qty change from trades since anchor."""
    df = trades_since_anchor(session, anchor_date)
    if df.empty:
        return {}
    deltas: dict[str, float] = {}
    for _, t in df.iterrows():
        sign = +1 if t["side"] == "Buy" else -1
        deltas[t["symbol"]] = deltas.get(t["symbol"], 0.0) + sign * t["qty"]
    return deltas


def cash_deltas_by_ccy(session, anchor_date: Optional[date]) -> dict[str, float]:
    """Per-ccy net cash change from trades + manual events since anchor."""
    deltas: dict[str, float] = {}
    for _, t in trades_since_anchor(session, anchor_date).iterrows():
        deltas[t["ccy"]] = deltas.get(t["ccy"], 0.0) + t["net_cash_impact"]
    for _, m in manual_events_since_anchor(session, anchor_date).iterrows():
        deltas[m["ccy"]] = deltas.get(m["ccy"], 0.0) + m["amount"]
    return deltas


def new_deposits_since_anchor_sgd(session, anchor_date: Optional[date]) -> float:
    """Net deposits since anchor (manual SGD-equivalent only). Excludes trade
    cash impacts (those are internal, not deposits/withdrawals)."""
    if not anchor_date:
        return 0.0
    total = 0.0
    for m in session.query(ManualCashEvent).filter(ManualCashEvent.event_date > anchor_date):
        total += m.amount * fx_at(session, m.ccy, m.event_date)
    return total


# ---------------------------------------------------------------------------
# Projection: anchor + delta
# ---------------------------------------------------------------------------

def _live_prices_by_symbol(session) -> dict[str, dict]:
    """Latest live price per symbol from live_position_snapshots (OpenAPI).
    Returns {symbol: {price, ccy, mv_local}}.
    """
    latest_pos_time = session.query(func.max(LivePositionSnapshot.snapshot_time)).scalar()
    out = {}
    if not latest_pos_time:
        return out
    for r in session.query(LivePositionSnapshot).filter_by(snapshot_time=latest_pos_time):
        out[r.symbol] = {
            "price": r.nominal_price or 0.0,
            "ccy": r.ccy,
            "mv_local": r.market_value or 0.0,
            "qty": r.qty or 0.0,
        }
    return out


def _cost_basis_for_position(session, symbol: str, current_qty: float,
                              anchor_pos: Optional[dict],
                              anchor_date: Optional[date]) -> tuple[float, float]:
    """Return (cost_basis_local, avg_cost_per_share_local) for the CURRENT
    holding of `symbol`, treating the anchor position as one big "buy" at
    the anchor price and layering on subsequent CSV trades using FIFO.
    """
    # Build a list of (qty_remaining, cost_per_share) lots
    lots = []
    if anchor_pos and anchor_pos["qty"] > 0:
        lots.append({
            "qty": anchor_pos["qty"],
            "cost_per_share": anchor_pos["price"] or 0.0,
        })
    if anchor_date:
        cutoff = datetime.combine(anchor_date, datetime.min.time())
        trades = (session.query(Trade)
                         .filter(Trade.symbol == symbol, Trade.fill_time > cutoff)
                         .order_by(Trade.fill_time))
        for t in trades:
            if not t.qty or t.qty <= 0:
                continue
            fee_ps = (t.fees_total or 0) / t.qty
            if t.side == "Buy":
                lots.append({
                    "qty": t.qty,
                    "cost_per_share": (t.fill_price or 0) + fee_ps,
                })
            else:  # Sell — close oldest lots FIFO
                remaining = t.qty
                for lot in lots:
                    if remaining <= 0:
                        break
                    if lot["qty"] <= 0:
                        continue
                    shares = min(lot["qty"], remaining)
                    lot["qty"] -= shares
                    remaining -= shares
                lots = [l for l in lots if l["qty"] > 1e-9]
    if current_qty <= 0:
        return 0.0, 0.0
    total_cost = sum(l["qty"] * l["cost_per_share"] for l in lots)
    total_qty = sum(l["qty"] for l in lots)
    avg = total_cost / total_qty if total_qty > 0 else 0.0
    return total_cost, avg


def projected_current_state(session) -> dict:
    """Anchored state forward-projected by all activity since anchor.

    Position prices use LIVE market data from live_position_snapshots when
    available, so unrealised gains/losses ARE captured. Funds (not returned
    by OpenAPI) still use anchor-statement prices.

    Per-position fields:
        qty             current quantity (anchor qty + trade deltas)
        qty_delta       change in qty since anchor (positive=net bought)
        anchor_price    price at last statement close (statement-derived)
        live_price      current price from OpenAPI (or anchor_price if missing)
        used_live_price True if we marked-to-market with a live quote
        mv_local        current qty × live_price (or anchor_price)
        fx_to_sgd       current FX rate to SGD
        mv_sgd          mv_local × fx_to_sgd
        cost_basis_local average cost in trade ccy (anchor pos treated as a
                         single buy at anchor price; subsequent trades FIFO)
        unrealised_pnl_local  mv_local − cost_basis_local
        unrealised_pnl_sgd    in SGD at current FX
    """
    anc = anchor_state(session)
    if not anc["anchor_date"]:
        return {"anchor_date": None}

    qty_deltas = position_qty_deltas(session, anc["anchor_date"])
    cash_deltas = cash_deltas_by_ccy(session, anc["anchor_date"])
    live_prices = _live_prices_by_symbol(session)

    projected_positions = []
    by_symbol = {p["symbol"]: p for p in anc["positions"]}

    # Positions held at anchor (possibly adjusted by trades since)
    for sym, p in by_symbol.items():
        delta = qty_deltas.pop(sym, 0.0)
        new_qty = (p["qty"] or 0) + delta
        if new_qty <= 1e-9:
            continue
        live = live_prices.get(sym)
        anchor_price = p["price"] or 0
        if live and live.get("price", 0) > 0:
            live_price = live["price"]
            used_live = True
        else:
            live_price = anchor_price
            used_live = False
        mv_local = new_qty * live_price
        rate = fx_at(session, p["ccy"], datetime.now())
        cost_basis_local, _avg = _cost_basis_for_position(
            session, sym, new_qty, p, anc["anchor_date"]
        )
        unreal_local = mv_local - cost_basis_local
        projected_positions.append({
            **p,
            "qty": new_qty, "qty_delta": delta,
            "anchor_price": anchor_price,
            "live_price": live_price,
            "used_live_price": used_live,
            "price": live_price,        # for back-compat with callers expecting `price`
            "mv_local": mv_local,
            "fx_to_sgd": rate,
            "mv_sgd": mv_local * rate,
            "cost_basis_local": cost_basis_local,
            "unrealised_pnl_local": unreal_local,
            "unrealised_pnl_sgd": unreal_local * rate,
        })

    # NEW positions bought after the anchor (didn't exist in anchor)
    for sym, delta in qty_deltas.items():
        if delta <= 0:
            continue
        live = live_prices.get(sym)
        last_trade = (session.query(Trade)
                             .filter(Trade.symbol == sym)
                             .order_by(Trade.fill_time.desc()).first())
        if not last_trade and not live:
            continue
        ccy = (live or {}).get("ccy") or (last_trade.ccy if last_trade else "USD")
        if live and live.get("price", 0) > 0:
            live_price = live["price"]; used_live = True
        elif last_trade:
            live_price = last_trade.fill_price; used_live = False
        else:
            live_price = 0; used_live = False
        mv_local = delta * (live_price or 0)
        rate = fx_at(session, ccy, datetime.now())
        cost_basis_local, _avg = _cost_basis_for_position(
            session, sym, delta, None, anc["anchor_date"]
        )
        unreal_local = mv_local - cost_basis_local
        projected_positions.append({
            "symbol": sym, "ccy": ccy,
            "exchange": last_trade.market if last_trade else "",
            "qty": delta, "qty_delta": delta,
            "anchor_price": None,
            "live_price": live_price,
            "used_live_price": used_live,
            "price": live_price,
            "mv_local": mv_local,
            "fx_to_sgd": rate,
            "mv_sgd": mv_local * rate,
            "cost_basis_local": cost_basis_local,
            "unrealised_pnl_local": unreal_local,
            "unrealised_pnl_sgd": unreal_local * rate,
            "is_fund": False,
        })

    # Cash
    projected_cash = {}
    for ccy, info in anc["cash_by_ccy"].items():
        d = cash_deltas.pop(ccy, 0.0)
        new_cash = info["cash"] + d
        rate = fx_at(session, ccy, datetime.now())
        projected_cash[ccy] = {
            "cash": new_cash, "cash_delta": d,
            "fx_to_sgd": rate, "cash_sgd": new_cash * rate,
        }
    for ccy, d in cash_deltas.items():
        rate = fx_at(session, ccy, datetime.now())
        projected_cash[ccy] = {
            "cash": d, "cash_delta": d,
            "fx_to_sgd": rate, "cash_sgd": d * rate,
        }

    cash_sgd = sum(c["cash_sgd"] for c in projected_cash.values())
    pos_sgd = sum(p["mv_sgd"] for p in projected_positions)
    new_dep = new_deposits_since_anchor_sgd(session, anc["anchor_date"])

    # Position-only unrealised P&L since anchor
    unrealised_pnl_sgd = sum(p.get("unrealised_pnl_sgd", 0) or 0 for p in projected_positions)
    # Anchor positions already had some unrealised P&L vs cost; what we want
    # to surface to the user is "how much have prices moved since anchor?",
    # which = current position MV − (anchor position MV adjusted by trade qty deltas
    # using anchor prices).
    anchor_pos_value_today_if_prices_flat = 0.0
    for p in projected_positions:
        anchor_pos_value_today_if_prices_flat += (
            (p["qty"] or 0) * (p.get("anchor_price") or 0) * (p.get("fx_to_sgd") or 1.0)
        )
    price_move_pnl_sgd = pos_sgd - anchor_pos_value_today_if_prices_flat

    return {
        "anchor_date": anc["anchor_date"],
        "anchor_nav_sgd": anc["nav_sgd"],
        "anchor_cumulative_deposits_sgd": anc["cumulative_deposits_sgd"],
        "anchor_pnl_sgd": anc["pnl_through_anchor_sgd"],

        "projected_cash_by_ccy": projected_cash,
        "projected_positions": projected_positions,
        "projected_cash_sgd": cash_sgd,
        "projected_positions_sgd": pos_sgd,
        "projected_nav_sgd": cash_sgd + pos_sgd,

        "deposits_since_anchor_sgd": new_dep,

        # True P&L since anchor with live mark-to-market:
        "since_anchor_pnl_sgd": (cash_sgd + pos_sgd) - anc["nav_sgd"] - new_dep,

        # Decomposition of since-anchor P&L:
        "unrealised_pnl_sgd": unrealised_pnl_sgd,          # current open positions' MV − cost basis
        "price_move_pnl_sgd": price_move_pnl_sgd,          # how much positions moved since anchor at live prices
    }


# ---------------------------------------------------------------------------
# Drift vs live (sanity check)
# ---------------------------------------------------------------------------

def projection_vs_live(session) -> pd.DataFrame:
    """Compare each per-ccy projected cash vs the LIVE API cash. Should be small."""
    proj = projected_current_state(session)
    if not proj.get("anchor_date"):
        return pd.DataFrame()
    latest_live_time = session.query(func.max(LiveCashSnapshot.snapshot_time)).scalar()
    live = {}
    if latest_live_time:
        for r in session.query(LiveCashSnapshot).filter_by(snapshot_time=latest_live_time):
            live[r.ccy] = r.cash or 0
    rows = []
    all_ccys = sorted(set(list(proj["projected_cash_by_ccy"]) + list(live)))
    for ccy in all_ccys:
        p = proj["projected_cash_by_ccy"].get(ccy, {}).get("cash", 0)
        l = live.get(ccy, 0)
        rows.append({"ccy": ccy, "projected_cash": p, "live_cash": l, "drift": l - p})
    return pd.DataFrame(rows)
