"""
Cost-basis tracking via FIFO lot matching.

Takes the `trades` table (one row per fill) and processes them chronologically
per symbol. Buys open lots; sells close oldest lots first.

Yields three views:
  open_lots         - lots you still hold, with cost basis in trade ccy
  closed_lots       - lots you've sold, with realised P&L in trade ccy and SGD
  attribution       - per current position, decomposes unrealised P&L into
                      security-return vs FX-return (in SGD)

These are computed on the fly each time (no new DB table) — the trade volume
is small enough that recomputing is instantaneous.
"""

from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

from src.db import Trade
from src.stats import fx_at, current_position_breakdown


@dataclass
class OpenLot:
    symbol: str
    ccy: str
    qty: float
    cost_per_share: float        # fill_price + pro-rata fees, in trade ccy
    fill_time: datetime


@dataclass
class ClosedLot:
    symbol: str
    ccy: str
    qty: float
    cost_per_share: float        # from the matched buy
    sell_price_per_share: float  # net of pro-rata sell fees, in trade ccy
    buy_time: datetime
    sell_time: datetime
    pnl_local: float             # in trade ccy
    pnl_sgd: float               # converted at sell-time FX


def _lots_for_symbol(session, symbol: str, trades: list[Trade]) -> tuple[list[OpenLot], list[ClosedLot]]:
    open_lots: list[OpenLot] = []
    closed_lots: list[ClosedLot] = []

    for t in sorted(trades, key=lambda x: x.fill_time):
        if not t.qty or t.qty <= 0:
            continue
        fee_per_share = (t.fees_total or 0) / t.qty

        if t.side == "Buy":
            open_lots.append(OpenLot(
                symbol=symbol, ccy=t.ccy, qty=t.qty,
                cost_per_share=(t.fill_price or 0) + fee_per_share,
                fill_time=t.fill_time,
            ))
            continue

        if t.side not in ("Sell", "Short Sell"):
            continue

        # Net price received per share after fees
        net_proceeds = (t.fill_price or 0) - fee_per_share
        remaining = t.qty
        for lot in open_lots:
            if remaining <= 0:
                break
            if lot.qty <= 0:
                continue
            shares = min(lot.qty, remaining)
            pnl_local = (net_proceeds - lot.cost_per_share) * shares
            pnl_sgd = pnl_local * fx_at(session, t.ccy, t.fill_time)
            closed_lots.append(ClosedLot(
                symbol=symbol, ccy=t.ccy, qty=shares,
                cost_per_share=lot.cost_per_share,
                sell_price_per_share=net_proceeds,
                buy_time=lot.fill_time,
                sell_time=t.fill_time,
                pnl_local=pnl_local, pnl_sgd=pnl_sgd,
            ))
            lot.qty -= shares
            remaining -= shares
        # If remaining > 0 here you sold more than you owned (short sale not
        # tracked in this MVP). Silently ignore the excess.

    # Drop fully-consumed open lots
    open_lots = [l for l in open_lots if l.qty > 1e-9]
    return open_lots, closed_lots


def compute_all_lots(session) -> dict[str, tuple[list[OpenLot], list[ClosedLot]]]:
    """Process every symbol's fills into open/closed lots."""
    by_symbol: dict[str, list[Trade]] = defaultdict(list)
    for t in session.query(Trade).all():
        by_symbol[t.symbol].append(t)
    return {sym: _lots_for_symbol(session, sym, trades)
            for sym, trades in by_symbol.items()}


def total_realised_pnl_sgd(session) -> float:
    """Sum of realised P&L across every closed lot since inception, in SGD."""
    total = 0.0
    for _, closed in compute_all_lots(session).values():
        total += sum(c.pnl_sgd for c in closed)
    return total


def closed_lots_table(session) -> pd.DataFrame:
    """All closed lots as a DataFrame, newest sale first."""
    rows = []
    for sym, (_, closed) in compute_all_lots(session).items():
        for c in closed:
            holding_days = (c.sell_time - c.buy_time).days
            rows.append({
                "symbol": sym,
                "ccy": c.ccy,
                "qty": c.qty,
                "buy_date": c.buy_time.date(),
                "sell_date": c.sell_time.date(),
                "days_held": holding_days,
                "cost": c.cost_per_share,
                "sell_price": c.sell_price_per_share,
                "pnl_local": c.pnl_local,
                "pnl_sgd": c.pnl_sgd,
            })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("sell_date", ascending=False).reset_index(drop=True)
    return df


def unrealised_pnl_attribution(session) -> pd.DataFrame:
    """
    For every currently-held position, decompose unrealised P&L (SGD) into:

        security_pnl_sgd = (current_value_local − cost_basis_local) × avg_buy_FX
        fx_pnl_sgd       = current_value_local × (current_FX − avg_buy_FX)
        total_pnl_sgd    = security_pnl_sgd + fx_pnl_sgd

    `avg_buy_FX` is weighted by lot cost basis. SGD positions have FX P&L = 0
    because both FX rates equal 1.
    """
    pos = current_position_breakdown(session)
    if pos.empty:
        return pd.DataFrame()

    lots_map = compute_all_lots(session)
    rows = []
    for _, p in pos.iterrows():
        symbol = p["symbol"]
        ccy = p["ccy"]
        open_lots, _ = lots_map.get(symbol, ([], []))
        current_fx = p["fx_to_sgd"] or fx_at(session, ccy, datetime.now())
        current_mv_local = p["mv_local"]

        if not open_lots:
            # CSV trades don't cover this position (e.g. fund units from statement only)
            rows.append({
                "symbol": symbol, "ccy": ccy,
                "qty": p["qty"],
                "cost_basis_local": None,
                "current_value_local": current_mv_local,
                "avg_buy_fx": None,
                "current_fx": current_fx,
                "security_pnl_sgd": None,
                "fx_pnl_sgd": None,
                "total_pnl_sgd": p["pl_sgd"],
            })
            continue

        total_qty = sum(l.qty for l in open_lots)
        cost_basis_local = sum(l.qty * l.cost_per_share for l in open_lots)
        avg_cost_per_share = cost_basis_local / total_qty if total_qty else 0.0

        # Weighted avg FX-at-buy, weighted by lot cost
        weighted_fx_num = sum(
            l.qty * l.cost_per_share * fx_at(session, ccy, l.fill_time)
            for l in open_lots
        )
        avg_buy_fx = weighted_fx_num / cost_basis_local if cost_basis_local else current_fx

        pl_local = current_mv_local - cost_basis_local
        security_pnl_sgd = pl_local * avg_buy_fx
        fx_pnl_sgd = current_mv_local * (current_fx - avg_buy_fx)
        total_pnl_sgd = security_pnl_sgd + fx_pnl_sgd

        rows.append({
            "symbol": symbol, "ccy": ccy,
            "qty": total_qty,
            "cost_basis_local": cost_basis_local,
            "current_value_local": current_mv_local,
            "avg_buy_fx": avg_buy_fx,
            "current_fx": current_fx,
            "security_pnl_sgd": security_pnl_sgd,
            "fx_pnl_sgd": fx_pnl_sgd,
            "total_pnl_sgd": total_pnl_sgd,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("total_pnl_sgd", ascending=False, na_position="last").reset_index(drop=True)
    return df
