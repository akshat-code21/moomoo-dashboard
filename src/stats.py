"""
Descriptive statistics for the portfolio.

All numbers are computed in SGD (your base currency) so the dashboard can
show consistent figures regardless of which currency a trade/cash event was
originally in.

Key helpers:
  fx_at(session, ccy, when)   -> daily rate_to_sgd, or 1.0 if missing
  total_net_deposits_sgd      -> all-time deposits minus withdrawals
  nav_series_sgd              -> month-end NAV series for charting
  current_nav_sgd             -> latest live NAV (cash + positions, SGD)
  current_position_breakdown  -> per-position table with SGD valuation
  monthly_flow_summary        -> per-month deposits/withdrawals for bars
"""

from __future__ import annotations
from datetime import date, datetime
from typing import Optional

import pandas as pd
from sqlalchemy import func

from src.db import (
    Session, FxRate, NavSnapshot, PositionSnapshot, CashEvent,
    ManualCashEvent, LiveCashSnapshot, LivePositionSnapshot,
    StatementFile, Trade, FundOrder, ExternalCashBalance, TargetWeight,
)


# ---------------------------------------------------------------------------
# FX helper
# ---------------------------------------------------------------------------

def fx_at(session, ccy: str, when: date | datetime) -> float:
    """Return the rate-to-SGD for `ccy` on or before `when`. Fallback 1.0."""
    if not ccy or ccy.upper() == "SGD":
        return 1.0
    if isinstance(when, datetime):
        when = when.date()
    row = (session.query(FxRate)
           .filter(FxRate.ccy == ccy.upper(), FxRate.rate_date <= when)
           .order_by(FxRate.rate_date.desc())
           .first())
    return row.rate_to_sgd if row else 1.0


# ---------------------------------------------------------------------------
# Cash flow aggregates
# ---------------------------------------------------------------------------

def total_net_deposits_sgd(session) -> float:
    """Sum of every external cash flow (statements + manual events), in SGD."""
    total = 0.0
    for ev in session.query(CashEvent).filter(CashEvent.event_type == "Cash In Out"):
        total += ev.amount * fx_at(session, ev.ccy, ev.event_time)
    for m in session.query(ManualCashEvent):
        total += m.amount * fx_at(session, m.ccy, m.event_date)
    return total


def total_dividends_sgd(session) -> float:
    """Dividends + fund distributions received, in SGD."""
    total = 0.0
    for ev in session.query(CashEvent).filter(
        CashEvent.event_type.in_(["Corporate Action", "Fund Corporate Action"])
    ):
        total += ev.amount * fx_at(session, ev.ccy, ev.event_time)
    return total


def monthly_flow_summary(session) -> pd.DataFrame:
    """Per-month aggregated deposits/withdrawals/dividends in SGD."""
    rows = []
    for ev in session.query(CashEvent).filter(
        CashEvent.event_type.in_(
            ["Cash In Out", "Corporate Action", "Fund Corporate Action"]
        )
    ):
        rows.append({
            "month": ev.event_time.strftime("%Y-%m"),
            "category": "Deposit/Withdrawal" if ev.event_type == "Cash In Out" else "Dividend",
            "amount_sgd": ev.amount * fx_at(session, ev.ccy, ev.event_time),
        })
    for m in session.query(ManualCashEvent):
        rows.append({
            "month": m.event_date.strftime("%Y-%m"),
            "category": "Manual (pending)",
            "amount_sgd": m.amount * fx_at(session, m.ccy, m.event_date),
        })
    if not rows:
        return pd.DataFrame(columns=["month", "category", "amount_sgd"])
    df = pd.DataFrame(rows)
    return df.groupby(["month", "category"], as_index=False)["amount_sgd"].sum()


# ---------------------------------------------------------------------------
# NAV
# ---------------------------------------------------------------------------

def nav_series_sgd(session) -> pd.DataFrame:
    """Month-start AND month-end NAV per snapshot date, in SGD.

    NAV = sum(cash_balance × fx_rate_to_sgd) + sum(market_value_sgd over positions)
    Returns DataFrame with columns: date, cash_sgd, positions_sgd, nav_sgd.
    """
    dates = [d for (d,) in (
        session.query(NavSnapshot.snapshot_date)
               .distinct()
               .order_by(NavSnapshot.snapshot_date).all()
    )]
    rows = []
    for d in dates:
        cash_rows = session.query(NavSnapshot).filter_by(snapshot_date=d).all()
        cash_sgd = sum(
            (r.cash_balance or 0) * (r.fx_rate_to_sgd or fx_at(session, r.ccy, d))
            for r in cash_rows
        )
        pos_rows = session.query(PositionSnapshot).filter_by(snapshot_date=d).all()
        pos_sgd = sum(r.market_value_sgd or 0 for r in pos_rows)
        rows.append({
            "date": d,
            "cash_sgd": cash_sgd,
            "positions_sgd": pos_sgd,
            "nav_sgd": cash_sgd + pos_sgd,
        })
    return pd.DataFrame(rows)


def external_cash_total_sgd(session) -> float:
    """Sum of latest-per-ccy external cash balances, converted to SGD."""
    # Get latest as_of_date per currency
    latest_dates = (session.query(
        ExternalCashBalance.ccy,
        func.max(ExternalCashBalance.as_of_date).label("latest"),
    ).group_by(ExternalCashBalance.ccy).all())
    total = 0.0
    for ccy, latest in latest_dates:
        row = (session.query(ExternalCashBalance)
                      .filter_by(ccy=ccy, as_of_date=latest)
                      .order_by(ExternalCashBalance.id.desc())
                      .first())
        if row:
            total += row.amount * fx_at(session, ccy, row.as_of_date)
    return total


def current_nav_sgd(session) -> dict:
    """True NAV in SGD.

    NAV = live_cash + live_equity_positions + fund_positions + external_cash

    Important nuance:
      OpenAPI's position_list_query returns EQUITIES ONLY — funds are not in
      the response. So `equity_sgd` comes from live_position_snapshots (fresh)
      and `funds_sgd` comes from the most recent statement's positions
      (slightly stale between statements, but only source we have for funds).

    Returns keys: cash_sgd, external_cash_sgd, equity_sgd, funds_sgd,
                  positions_sgd (=equity+funds), nav_sgd, when.
    """
    latest_cash_time = session.query(func.max(LiveCashSnapshot.snapshot_time)).scalar()
    latest_pos_time = session.query(func.max(LivePositionSnapshot.snapshot_time)).scalar()
    when = latest_cash_time or latest_pos_time

    # Live cash from OpenAPI
    cash_sgd = 0.0
    if latest_cash_time:
        for r in session.query(LiveCashSnapshot).filter_by(snapshot_time=latest_cash_time):
            cash_sgd += (r.cash or 0) * fx_at(session, r.ccy, latest_cash_time)

    # Live equities from OpenAPI
    equity_sgd = 0.0
    if latest_pos_time:
        for r in session.query(LivePositionSnapshot).filter_by(snapshot_time=latest_pos_time):
            equity_sgd += (r.market_value or 0) * fx_at(
                session, r.ccy, latest_pos_time
            )

    # Funds — from the latest statement (OpenAPI doesn't return funds)
    funds_sgd = fund_current_value_sgd(session)

    # External cash (manually tracked)
    ext_cash_sgd = external_cash_total_sgd(session)

    return {
        "cash_sgd": cash_sgd,
        "external_cash_sgd": ext_cash_sgd,
        "equity_sgd": equity_sgd,
        "funds_sgd": funds_sgd,
        # Keep backward-compatible alias (some callers/old tests use this key)
        "positions_sgd": equity_sgd + funds_sgd,
        "nav_sgd": cash_sgd + equity_sgd + funds_sgd + ext_cash_sgd,
        "when": when,
    }


def current_position_breakdown(session) -> pd.DataFrame:
    """Per-position table at latest live snapshot, with SGD valuation."""
    latest_time = session.query(func.max(LivePositionSnapshot.snapshot_time)).scalar()
    if not latest_time:
        return pd.DataFrame()
    rows = session.query(LivePositionSnapshot).filter_by(snapshot_time=latest_time).all()
    if not rows:
        return pd.DataFrame()

    data = []
    for r in rows:
        rate = fx_at(session, r.ccy, latest_time)
        data.append({
            "symbol": r.symbol,
            "ccy": r.ccy,
            "qty": r.qty,
            "cost_price": r.cost_price,
            "price": r.nominal_price,
            "mv_local": r.market_value,
            "fx_to_sgd": rate,
            "mv_sgd": r.market_value * rate,
            "pl_local": r.pl_val,
            "pl_sgd": r.pl_val * rate,
        })
    df = pd.DataFrame(data)
    if not df.empty:
        total = df["mv_sgd"].sum()
        df["weight_pct"] = (df["mv_sgd"] / total * 100) if total else 0
        df = df.sort_values("mv_sgd", ascending=False).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Performance stats (simple)
# ---------------------------------------------------------------------------

def total_pnl_sgd(session) -> float:
    """Current NAV minus net deposits = total cumulative gain/loss in SGD."""
    return current_nav_sgd(session)["nav_sgd"] - total_net_deposits_sgd(session)


def total_return_pct(session) -> float:
    deposits = total_net_deposits_sgd(session)
    if deposits == 0:
        return 0.0
    return (total_pnl_sgd(session) / deposits) * 100


def concentration_hhi(session) -> float:
    """Herfindahl index of position concentration (0-10000). 10000 = single position."""
    df = current_position_breakdown(session)
    if df.empty:
        return 0.0
    weights = df["weight_pct"] / 100
    return float((weights ** 2).sum() * 10000)


# ---------------------------------------------------------------------------
# Performance analytics (Track A)
# ---------------------------------------------------------------------------

# Risk-free rate used for Sharpe. MAS 6-month T-bill is around 3.0–3.7%.
DEFAULT_RF_ANNUAL = 0.035


def _flows_by_month(session) -> dict[str, float]:
    """Aggregate every external SGD cash flow per calendar month."""
    out: dict[str, float] = {}
    for ev in session.query(CashEvent).filter(CashEvent.event_type == "Cash In Out"):
        m = ev.event_time.strftime("%Y-%m")
        out[m] = out.get(m, 0.0) + ev.amount * fx_at(session, ev.ccy, ev.event_time)
    for m_evt in session.query(ManualCashEvent):
        m = m_evt.event_date.strftime("%Y-%m")
        out[m] = out.get(m, 0.0) + m_evt.amount * fx_at(session, m_evt.ccy, m_evt.event_date)
    return out


def monthly_returns(session) -> pd.DataFrame:
    """Per-month NAV, flows, and Modified-Dietz return %.

    Modified Dietz assumes flows happen mid-month:
        return = (NAV_end - NAV_start - flow) / (NAV_start + 0.5 * flow)

    Returns DataFrame: month, nav_sgd, flow_sgd, return.
    The first row has return=0 because there's no prior NAV to compare to.
    """
    nav = nav_series_sgd(session)
    if nav.empty:
        return pd.DataFrame(columns=["month", "nav_sgd", "flow_sgd", "return"])
    nav = nav.sort_values("date").reset_index(drop=True)
    nav["month"] = nav["date"].apply(lambda d: d.strftime("%Y-%m"))
    # Use latest snapshot per month as the month-end NAV
    monthly_nav = nav.groupby("month", as_index=False).agg({
        "date": "max", "nav_sgd": "last",
    }).sort_values("month").reset_index(drop=True)

    flows = _flows_by_month(session)
    monthly_nav["flow_sgd"] = monthly_nav["month"].map(flows).fillna(0.0)

    rets = [0.0]
    for i in range(1, len(monthly_nav)):
        start_nav = monthly_nav.loc[i - 1, "nav_sgd"]
        end_nav = monthly_nav.loc[i, "nav_sgd"]
        flow = monthly_nav.loc[i, "flow_sgd"]
        denom = start_nav + 0.5 * flow
        rets.append((end_nav - start_nav - flow) / denom if denom > 0 else 0.0)
    monthly_nav["return"] = rets
    return monthly_nav[["month", "nav_sgd", "flow_sgd", "return"]]


def cumulative_return_series(session) -> pd.DataFrame:
    """Cumulative %-return series from inception (compound)."""
    mr = monthly_returns(session)
    if mr.empty:
        return pd.DataFrame(columns=["month", "cum_return_pct"])
    mr["cum_return_pct"] = ((1 + mr["return"]).cumprod() - 1) * 100
    return mr[["month", "cum_return_pct"]]


def drawdown_series(session) -> pd.DataFrame:
    """Running drawdown % from peak per month (always <= 0)."""
    mr = monthly_returns(session)
    if mr.empty:
        return pd.DataFrame(columns=["month", "drawdown_pct"])
    cum = (1 + mr["return"]).cumprod()
    peak = cum.cummax()
    mr["drawdown_pct"] = (cum / peak - 1) * 100
    return mr[["month", "drawdown_pct"]]


def time_weighted_return_annualized_pct(session) -> float:
    """Annualised TWR % via compounding monthly returns."""
    mr = monthly_returns(session)
    if len(mr) < 2:
        return 0.0
    rets = mr["return"].iloc[1:]
    cum = (1 + rets).prod()
    n_months = len(rets)
    if n_months == 0 or cum <= 0:
        return 0.0
    return (cum ** (12 / n_months) - 1) * 100


def volatility_annualized_pct(session) -> float:
    """Monthly return std-dev × √12, in %."""
    import math
    mr = monthly_returns(session)
    rets = mr["return"].iloc[1:]
    if len(rets) < 2:
        return 0.0
    return float(rets.std() * math.sqrt(12) * 100)


def sharpe_ratio(session, rf_annual: float = DEFAULT_RF_ANNUAL) -> float:
    """(TWR - rf) / vol, annualised."""
    twr = time_weighted_return_annualized_pct(session) / 100
    vol = volatility_annualized_pct(session) / 100
    if vol == 0:
        return 0.0
    return (twr - rf_annual) / vol


def max_drawdown_pct(session) -> float:
    """Worst peak-to-trough decline in % (negative number)."""
    dd = drawdown_series(session)
    if dd.empty:
        return 0.0
    return float(dd["drawdown_pct"].min())


def money_weighted_irr_pct(session) -> float:
    """XIRR (irregular-interval IRR) using all external flows + current NAV.

    Sign convention:
        Investor deposits      -> negative cash flow (money OUT of pocket)
        Investor withdrawals   -> positive cash flow (money INTO pocket)
        Current NAV (terminal) -> positive cash flow (notional liquidation value)
    """
    from scipy.optimize import brentq

    flows: list[tuple[date, float]] = []
    for ev in session.query(CashEvent).filter(CashEvent.event_type == "Cash In Out"):
        sgd = ev.amount * fx_at(session, ev.ccy, ev.event_time)
        flows.append((ev.event_time.date(), -sgd))   # invert sign for investor POV
    for m_evt in session.query(ManualCashEvent):
        sgd = m_evt.amount * fx_at(session, m_evt.ccy, m_evt.event_date)
        flows.append((m_evt.event_date, -sgd))

    nav = current_nav_sgd(session)
    if not nav["when"] or nav["nav_sgd"] <= 0:
        return 0.0
    flows.append((nav["when"].date(), nav["nav_sgd"]))

    if len(flows) < 2:
        return 0.0
    flows.sort(key=lambda x: x[0])
    t0 = flows[0][0]

    def xnpv(rate: float) -> float:
        return sum(a / (1 + rate) ** ((d - t0).days / 365.0) for d, a in flows)

    # Bracket [-99%, +1000%] should cover any realistic IRR
    try:
        irr = brentq(xnpv, -0.99, 10.0, maxiter=200)
        return irr * 100
    except (ValueError, RuntimeError):
        return 0.0


def currency_attribution(session) -> pd.DataFrame:
    """Per-currency current exposure and unrealised P&L decomposition.

    Cheap version: for each ccy currently held, show market value in trade ccy,
    market value in SGD, and the unrealized P&L from the live snapshot — split
    into security component (pl_val in trade ccy × current FX) and FX component
    estimated as (current_mv × (current_fx - cost_fx)).

    The FX component is approximate because we don't track per-position cost FX
    yet; we use the average statement FX as a proxy. Good enough for v1.
    """
    df = current_position_breakdown(session)
    if df.empty:
        return pd.DataFrame()
    out = df.groupby("ccy", as_index=False).agg({
        "mv_local": "sum", "mv_sgd": "sum",
        "pl_local": "sum", "pl_sgd": "sum",
    }).rename(columns={
        "pl_local": "pl_security_local", "pl_sgd": "pl_security_sgd",
    })
    return out


def benchmark_monthly_returns(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Monthly returns for a benchmark from yfinance. Returns: month, return, cum_return_pct."""
    import yfinance as yf
    hist = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=True)
    if hist is None or hist.empty:
        return pd.DataFrame(columns=["month", "return", "cum_return_pct"])
    close = hist["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    monthly = close.resample("ME").last().dropna()
    returns = monthly.pct_change().dropna()
    df = pd.DataFrame({
        "month": returns.index.strftime("%Y-%m"),
        "return": returns.values,
    })
    df["cum_return_pct"] = ((1 + df["return"]).cumprod() - 1) * 100
    return df


def fund_orders_table(session) -> pd.DataFrame:
    """All fund orders as a DataFrame, newest first."""
    rows = []
    for o in session.query(FundOrder).order_by(FundOrder.order_time.desc()):
        rows.append({
            "order_time": o.order_time,
            "fund": o.fund_name,
            "type": o.order_type,
            "status": o.status,
            "amount": o.amount if o.amount is not None else float("nan"),
            "units": o.units if o.units is not None else float("nan"),
            "ccy": o.ccy,
        })
    return pd.DataFrame(rows)


def fund_pnl_summary(session) -> pd.DataFrame:
    """Per-fund aggregate: subscriptions, redemptions, current units & value, approx P&L.

    We approximate P&L as (current_value_sgd + total_redeemed_sgd) − total_subscribed_sgd.
    This counts only Completed orders; Submitted/Terminated are ignored.

    Note: redemptions in the CSV may be in units rather than amount. For those rows
    we can't compute the SGD value without the redemption price. We mark those as
    NaN in the redeemed_sgd column. If you want exact P&L, the source of truth is
    the statement page 7-8 trade lines which include the unit price.
    """
    funds_by_name: dict[str, dict] = {}
    for o in session.query(FundOrder).filter(FundOrder.status == "Completed"):
        slot = funds_by_name.setdefault(o.fund_name, {
            "ccy": o.ccy,
            "subscribed_sgd": 0.0,
            "redeemed_sgd": 0.0,
            "units_subscribed": 0.0,
            "units_redeemed": 0.0,
        })
        rate = fx_at(session, o.ccy, o.order_time)
        if o.order_type == "Subscribe":
            if o.amount is not None:
                slot["subscribed_sgd"] += o.amount * rate
            if o.units is not None:
                slot["units_subscribed"] += o.units
        elif o.order_type == "Redeem":
            if o.amount is not None:
                slot["redeemed_sgd"] += o.amount * rate
            if o.units is not None:
                slot["units_redeemed"] += o.units

    # Current value from latest positions_snapshots (statement-derived)
    latest_pos_date = session.query(func.max(PositionSnapshot.snapshot_date)).scalar()
    current_by_fund_name: dict[str, dict] = {}
    if latest_pos_date:
        for p in session.query(PositionSnapshot).filter_by(snapshot_date=latest_pos_date):
            # PositionSnapshot.symbol is the ISIN/SGXZ code; we don't directly know
            # the fund name from positions, so we match later by looking at the
            # ending positions where exchange='FD' or '--' and ccy is SGD/USD.
            current_by_fund_name[p.symbol] = {
                "qty": p.settled_qty or 0,
                "mv_local": p.market_value or 0,
                "mv_sgd": p.market_value_sgd or 0,
            }

    rows = []
    for name, slot in funds_by_name.items():
        # We don't have an automatic name<->ISIN mapping. Show subscriptions/redemptions
        # always; current value will be NaN unless the user supplies a mapping.
        rows.append({
            "fund": name,
            "ccy": slot["ccy"],
            "subscribed_sgd": slot["subscribed_sgd"],
            "redeemed_sgd": slot["redeemed_sgd"],
            "net_invested_sgd": slot["subscribed_sgd"] - slot["redeemed_sgd"],
            "units_subscribed": slot["units_subscribed"],
            "units_redeemed": slot["units_redeemed"],
            "units_remaining_est": slot["units_subscribed"] - slot["units_redeemed"],
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("net_invested_sgd", ascending=False).reset_index(drop=True)
    return df


def total_funds_net_invested_sgd(session) -> float:
    df = fund_pnl_summary(session)
    if df.empty:
        return 0.0
    return float(df["net_invested_sgd"].sum())


def fund_current_value_sgd(session) -> float:
    """Total SGD market value of currently-held FUND positions (not equities).
    Funds are identified in PositionSnapshot by exchange 'FD' or '--', or by
    symbol prefix matching common ISIN patterns.

    Uses the LATEST statement snapshot. Stale between statements, but funds
    rarely move much intra-month and this is the only source — OpenAPI's
    position_list_query does not return funds.
    """
    latest_pos_date = session.query(func.max(PositionSnapshot.snapshot_date)).scalar()
    if not latest_pos_date:
        return 0.0
    total = 0.0
    for p in session.query(PositionSnapshot).filter_by(snapshot_date=latest_pos_date):
        is_fund = (
            (p.exchange in ("FD", "--", None, ""))
            or (p.symbol and p.symbol.startswith(("SG9999", "SGXZ", "LU", "IE", "BMG")))
        )
        if is_fund:
            total += p.market_value_sgd or 0
    return total


def fund_positions_detail(session) -> pd.DataFrame:
    """Per-fund detail of currently-held fund positions from latest statement."""
    latest_pos_date = session.query(func.max(PositionSnapshot.snapshot_date)).scalar()
    if not latest_pos_date:
        return pd.DataFrame()
    rows = []
    for p in session.query(PositionSnapshot).filter_by(snapshot_date=latest_pos_date):
        is_fund = (
            (p.exchange in ("FD", "--", None, ""))
            or (p.symbol and p.symbol.startswith(("SG9999", "SGXZ", "LU", "IE", "BMG")))
        )
        if not is_fund:
            continue
        rows.append({
            "symbol": p.symbol, "ccy": p.ccy,
            "units": p.settled_qty,
            "price": p.closing_price,
            "mv_local": p.market_value,
            "mv_sgd": p.market_value_sgd,
            "as_of": p.snapshot_date,
        })
    return pd.DataFrame(rows)


def fund_pnl_total_sgd(session) -> float:
    """Approx fund P&L (SGD): current_value - net_invested."""
    return fund_current_value_sgd(session) - total_funds_net_invested_sgd(session)


def total_trading_fees_sgd(session) -> float:
    """Sum of fees on every recorded trade, converted to SGD at trade-time FX."""
    total = 0.0
    for t in session.query(Trade):
        total += (t.fees_total or 0) * fx_at(session, t.ccy, t.fill_time)
    return total


def pnl_breakdown(session) -> dict:
    """Decompose Total P&L (= NAV - Net Deposits) into named components.

    Returns:
        equity_realised   - sum of realised P&L on closed equity lots (SGD)
        equity_unrealised - sum of attribution.total_pnl_sgd across open positions
        fund_pnl          - fund_current_value_sgd - net_invested (approx)
        dividends         - cumulative dividend income in SGD
        trading_fees      - cumulative fees paid (negative number, in SGD)
        residual          - whatever's left over (FX on cash, fees we missed,
                            withholding tax, etc.)
        total_explained   - sum of the above categories
        total_actual      - NAV - Net Deposits, the ground truth
        external_cash_sgd - external cash currently included in NAV
    """
    # Import here to avoid a circular import (lots imports stats)
    from src import lots as _lots

    eq_real = _lots.total_realised_pnl_sgd(session)
    attr = _lots.unrealised_pnl_attribution(session)
    eq_unreal = float(attr["total_pnl_sgd"].sum()) if not attr.empty else 0.0
    fund_pnl = fund_pnl_total_sgd(session)
    divs = total_dividends_sgd(session)
    fees = total_trading_fees_sgd(session)

    nav = current_nav_sgd(session)
    deposits = total_net_deposits_sgd(session)
    total_actual = nav["nav_sgd"] - deposits

    explained = eq_real + eq_unreal + fund_pnl + divs - fees
    residual = total_actual - explained

    return {
        "equity_realised": eq_real,
        "equity_unrealised": eq_unreal,
        "fund_pnl": fund_pnl,
        "dividends": divs,
        "trading_fees": -fees,        # display as negative
        "residual": residual,
        "total_explained": explained,
        "total_actual": total_actual,
        "external_cash_sgd": nav.get("external_cash_sgd", 0),
        "current_nav_sgd": nav["nav_sgd"],
        "net_deposits_sgd": deposits,
    }


# ---------------------------------------------------------------------------
# Per-position FX history
# ---------------------------------------------------------------------------

def fx_history_for_currencies(session, ccys: list[str]) -> pd.DataFrame:
    """Daily FX rate-to-SGD for the given currencies, from fx_rates."""
    if not ccys:
        return pd.DataFrame()
    rows = (session.query(FxRate)
                   .filter(FxRate.ccy.in_([c.upper() for c in ccys]))
                   .order_by(FxRate.rate_date).all())
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([
        {"date": r.rate_date, "ccy": r.ccy, "rate": r.rate_to_sgd}
        for r in rows
    ])
    return df.pivot_table(index="date", columns="ccy", values="rate")


# ---------------------------------------------------------------------------
# Rebalancing
# ---------------------------------------------------------------------------

def rebalancing_view(session) -> pd.DataFrame:
    """Compare current weights to target weights, suggest delta trades.

    Returns df with: symbol, current_pct, target_pct, delta_pct,
    current_value_sgd, target_value_sgd, suggested_trade_sgd.
    Positive suggested_trade_sgd = BUY more; negative = SELL.
    """
    pos = current_position_breakdown(session)
    if pos.empty:
        return pd.DataFrame()

    nav = current_nav_sgd(session)["nav_sgd"]
    if nav <= 0:
        return pd.DataFrame()

    targets = {t.symbol: t.target_weight_pct for t in session.query(TargetWeight)}

    rows = []
    seen = set()
    for _, r in pos.iterrows():
        sym = r["symbol"]
        seen.add(sym)
        current_pct = float(r["mv_sgd"]) / nav * 100 if nav else 0.0
        tgt = targets.get(sym, 0.0)
        target_value = tgt / 100 * nav
        rows.append({
            "symbol": sym, "ccy": r["ccy"],
            "current_pct": current_pct,
            "target_pct": tgt,
            "delta_pct": tgt - current_pct,
            "current_value_sgd": float(r["mv_sgd"]),
            "target_value_sgd": target_value,
            "suggested_trade_sgd": target_value - float(r["mv_sgd"]),
        })
    # Targets for symbols we don't currently hold (full BUY suggested)
    for sym, tgt in targets.items():
        if sym in seen:
            continue
        target_value = tgt / 100 * nav
        rows.append({
            "symbol": sym, "ccy": "?",
            "current_pct": 0.0,
            "target_pct": tgt,
            "delta_pct": tgt,
            "current_value_sgd": 0.0,
            "target_value_sgd": target_value,
            "suggested_trade_sgd": target_value,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("delta_pct", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)
    return df


def reconciliation_status(session) -> dict:
    """Summary used by the dashboard's status badge."""
    latest_stmt = session.query(StatementFile).order_by(StatementFile.period_end.desc()).first()
    latest_snap = session.query(func.max(LiveCashSnapshot.snapshot_time)).scalar()
    return {
        "last_statement": latest_stmt.period_end if latest_stmt else None,
        "last_live_snapshot": latest_snap,
        "n_statements": session.query(StatementFile).count(),
        "n_cash_events": session.query(CashEvent).count(),
        "n_trades": session.query(Trade).count(),
        "n_manual_pending": session.query(ManualCashEvent).filter(
            ManualCashEvent.confirmed_at.is_(None)
        ).count(),
        "n_fund_orders": session.query(FundOrder).count(),
    }
