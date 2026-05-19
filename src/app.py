"""
Streamlit dashboard for the Moomoo portfolio.

Launch:
    streamlit run src/app.py
or:
    bash run_dashboard.sh

Five tabs:
    Overview       - headline numbers, reconciliation badge
    NAV History    - month-end NAV chart + monthly flow bars
    Positions      - live position breakdown with SGD valuation
    Capital Flows  - timeline of every deposit/withdrawal/dividend
    Trades         - filterable trade history from CSV
    Log Event      - form to add manual deposit / withdrawal / FX
"""

from __future__ import annotations
import sys
from datetime import datetime, date
from pathlib import Path

# Make the project importable when run via `streamlit run src/app.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from src.db import (
    Session, CashEvent, ManualCashEvent, Trade, StatementFile,
    ExternalCashBalance, TargetWeight,
    create_all as _db_create_all,
)
from src import stats, lots, anchor as _anchor

# Ensure any tables added since the DB was first created exist.
# create_all only creates missing tables; never touches existing ones.
_db_create_all()


st.set_page_config(
    page_title="Moomoo Portfolio",
    page_icon=":bar_chart:",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Lightweight typographic polish — bigger headlines, calmer dividers, denser tables.
st.markdown("""
<style>
  h1 { font-weight: 700; letter-spacing: -0.02em; margin-bottom: 0.25rem; }
  h2 { font-weight: 600; letter-spacing: -0.01em; margin-top: 1.25rem; }
  h3 { font-weight: 600; color: #94a3b8; margin-top: 1rem; }
  [data-testid="stMetricValue"] { font-weight: 700; font-size: 1.6rem; }
  [data-testid="stMetricLabel"] { color: #94a3b8; font-size: 0.85rem; }
  [data-testid="stMetricDelta"] { font-size: 0.85rem; }
  .stDataFrame { font-size: 0.9rem; }
  hr { border-color: #1e293b !important; margin: 1.25rem 0 !important; }
  .stCaption, [data-testid="stCaptionContainer"] { color: #64748b; }
</style>
""", unsafe_allow_html=True)

st.title("Moomoo Portfolio")
st.caption("Statement-anchored portfolio tracking · monthly statements · trade CSV · OpenAPI live data")


# ---------------------------------------------------------------------------
# Cached data access
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def load_overview():
    with Session() as session:
        recon = stats.reconciliation_status(session)
        nav = stats.current_nav_sgd(session)
        return {
            "current_nav": nav,
            "net_deposits": stats.total_net_deposits_sgd(session),
            "total_pnl": stats.total_pnl_sgd(session),
            "return_pct": stats.total_return_pct(session),
            "dividends": stats.total_dividends_sgd(session),
            "hhi": stats.concentration_hhi(session),
            "realised_pnl": lots.total_realised_pnl_sgd(session),
            "recon": recon,
        }


@st.cache_data(ttl=60)
def load_attribution():
    with Session() as session:
        return lots.unrealised_pnl_attribution(session)


@st.cache_data(ttl=60)
def load_closed_lots():
    with Session() as session:
        return lots.closed_lots_table(session)


@st.cache_data(ttl=60)
def load_fund_orders():
    with Session() as session:
        return stats.fund_orders_table(session)


@st.cache_data(ttl=60)
def load_fund_summary():
    with Session() as session:
        return stats.fund_pnl_summary(session)


@st.cache_data(ttl=60)
def load_pnl_breakdown():
    with Session() as session:
        return stats.pnl_breakdown(session)


@st.cache_data(ttl=60)
def load_anchor():
    with Session() as session:
        return _anchor.anchor_state(session)


@st.cache_data(ttl=60)
def load_projection():
    with Session() as session:
        return _anchor.projected_current_state(session)


@st.cache_data(ttl=60)
def load_trades_since_anchor(anchor_date):
    with Session() as session:
        return _anchor.trades_since_anchor(session, anchor_date)


@st.cache_data(ttl=60)
def load_funds_since_anchor(anchor_date):
    with Session() as session:
        return _anchor.fund_orders_since_anchor(session, anchor_date)


@st.cache_data(ttl=60)
def load_manuals_since_anchor(anchor_date):
    with Session() as session:
        return _anchor.manual_events_since_anchor(session, anchor_date)


@st.cache_data(ttl=60)
def load_projection_vs_live():
    with Session() as session:
        return _anchor.projection_vs_live(session)


@st.cache_data(ttl=60)
def load_rebalance():
    with Session() as session:
        return stats.rebalancing_view(session)


@st.cache_data(ttl=300)
def load_fx_history(ccys_tuple):
    with Session() as session:
        return stats.fx_history_for_currencies(session, list(ccys_tuple))


@st.cache_data(ttl=60)
def load_fund_orders():
    with Session() as session:
        return stats.fund_orders_table(session)


@st.cache_data(ttl=60)
def load_fund_pnl():
    with Session() as session:
        return stats.fund_pnl_summary(session)


@st.cache_data(ttl=60)
def load_nav_series():
    with Session() as session:
        return stats.nav_series_sgd(session)


@st.cache_data(ttl=60)
def load_monthly_flows():
    with Session() as session:
        return stats.monthly_flow_summary(session)


@st.cache_data(ttl=60)
def load_positions():
    with Session() as session:
        return stats.current_position_breakdown(session)


@st.cache_data(ttl=60)
def load_cash_events():
    with Session() as session:
        from collections import namedtuple
        Row = namedtuple("Row", "event_time ccy event_type amount comment source")
        rows = []
        for e in session.query(CashEvent).order_by(CashEvent.event_time):
            rows.append({
                "when": e.event_time, "ccy": e.ccy, "type": e.event_type,
                "amount": e.amount, "memo": (e.comment or "")[:60],
                "source": "statement",
            })
        for m in session.query(ManualCashEvent).order_by(ManualCashEvent.event_date):
            rows.append({
                "when": datetime.combine(m.event_date, datetime.min.time()),
                "ccy": m.ccy, "type": "Manual",
                "amount": m.amount, "memo": (m.memo or "")[:60],
                "source": "manual",
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("when", ascending=False).reset_index(drop=True)
        return df


@st.cache_data(ttl=60)
def load_trades():
    with Session() as session:
        rows = []
        for t in session.query(Trade).order_by(Trade.fill_time.desc()):
            rows.append({
                "when": t.fill_time, "symbol": t.symbol, "side": t.side,
                "qty": t.qty, "price": t.fill_price, "fees": t.fees_total,
                "net_cash": t.net_cash_impact, "ccy": t.ccy, "market": t.market,
            })
        return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def load_stats_block():
    with Session() as session:
        return {
            "twr_pct": stats.time_weighted_return_annualized_pct(session),
            "irr_pct": stats.money_weighted_irr_pct(session),
            "vol_pct": stats.volatility_annualized_pct(session),
            "sharpe": stats.sharpe_ratio(session),
            "max_dd_pct": stats.max_drawdown_pct(session),
            "monthly": stats.monthly_returns(session),
            "cum_return": stats.cumulative_return_series(session),
            "drawdown": stats.drawdown_series(session),
            "ccy_attribution": stats.currency_attribution(session),
        }


@st.cache_data(ttl=3600)
def load_benchmark(symbol: str, start_str: str, end_str: str):
    from datetime import date as _date
    return stats.benchmark_monthly_returns(
        symbol, _date.fromisoformat(start_str), _date.fromisoformat(end_str)
    )


def refresh_caches():
    load_overview.clear()
    load_nav_series.clear()
    load_monthly_flows.clear()
    load_positions.clear()
    load_cash_events.clear()
    load_trades.clear()
    load_stats_block.clear()
    load_attribution.clear()
    load_closed_lots.clear()
    load_fund_orders.clear()
    load_fund_summary.clear()
    load_pnl_breakdown.clear()
    load_rebalance.clear()
    load_fx_history.clear()
    load_anchor.clear()
    load_projection.clear()
    load_trades_since_anchor.clear()
    load_funds_since_anchor.clear()
    load_manuals_since_anchor.clear()
    load_projection_vs_live.clear()
    load_fund_orders.clear()
    load_fund_pnl.clear()


# Sidebar: refresh button
with st.sidebar:
    st.header("Controls")
    if st.button("Refresh from DB"):
        refresh_caches()
        st.success("Caches cleared. Reload the page.")
    st.caption("Run `python3 run_phase2.py` in your terminal to pull live data, "
               "then click Refresh.")


tabs = st.tabs([
    "Overview", "Since Anchor", "NAV History", "Statistics", "Positions",
    "Capital Flows", "Trades", "Funds", "Rebalance", "Log Event",
])

# ===========================================================================
# Overview
# ===========================================================================

with tabs[0]:
    anc = load_anchor()
    proj = load_projection()
    d = load_overview()

    if not anc.get("anchor_date"):
        st.info("Ingest at least one monthly statement to see the anchor view.")
    else:
        st.caption(f"**Anchored to:** `{anc['anchor_filename']}` "
                   f"(period end {anc['anchor_date']}). "
                   f"Everything below = anchor + activity since that date.")

        # ---- Hero row: 4 headline cards with color-coded deltas ----
        mtm_pnl = sum(p.get("unrealised_pnl_sgd", 0) or 0
                       for p in proj["projected_positions"])
        nav_delta = proj['projected_nav_sgd'] - anc['nav_sgd']

        c1, c2, c3, c4 = st.columns(4)
        c1.metric(
            f"NAV at anchor · {anc['anchor_date']}",
            f"S$ {anc['nav_sgd']:,.2f}",
            help="Net Asset Value at the last statement date, directly from Moomoo. "
                 "Fully reconciled — no estimates."
        )
        c2.metric(
            "Projected NAV today",
            f"S$ {proj['projected_nav_sgd']:,.2f}",
            delta=f"{nav_delta:+,.2f}",
            delta_color="normal",
            help="Anchor + cash changes + positions at LIVE prices."
        )
        c3.metric(
            "P&L through anchor",
            f"S$ {anc['pnl_through_anchor_sgd']:+,.2f}",
            delta_color="off",
            help="Anchor NAV minus cumulative deposits through anchor. "
                 "What Moomoo officially says you'd made by the anchor date."
        )
        c4.metric(
            "Mark-to-market P&L · open positions",
            f"S$ {mtm_pnl:+,.2f}",
            delta_color="off",
            help="Sum across every currently-held position of "
                 "(live_price − cost_basis_per_share) × qty × current_FX. "
                 "Pure price movement vs your cost basis."
        )

        st.divider()

        # Anchor cash breakdown
        st.subheader(f"Cash at anchor ({anc['anchor_date']}) → projected today")
        cash_rows = []
        for ccy, info in anc["cash_by_ccy"].items():
            proj_info = proj["projected_cash_by_ccy"].get(ccy, {})
            cash_rows.append({
                "ccy": ccy,
                "anchor_cash": info["cash"],
                "delta_since": proj_info.get("cash_delta", 0.0),
                "projected_cash": proj_info.get("cash", info["cash"]),
                "fx_to_sgd": proj_info.get("fx_to_sgd", info["fx_to_sgd"]),
                "projected_sgd": proj_info.get("cash_sgd", info["cash_sgd"]),
            })
        # New ccys
        for ccy, p in proj["projected_cash_by_ccy"].items():
            if ccy not in anc["cash_by_ccy"]:
                cash_rows.append({
                    "ccy": ccy,
                    "anchor_cash": 0.0,
                    "delta_since": p["cash_delta"],
                    "projected_cash": p["cash"],
                    "fx_to_sgd": p["fx_to_sgd"],
                    "projected_sgd": p["cash_sgd"],
                })
        cash_df = pd.DataFrame(cash_rows)
        if not cash_df.empty:
            disp = cash_df.copy()
            for c in ["anchor_cash", "delta_since", "projected_cash", "projected_sgd"]:
                disp[c] = disp[c].apply(lambda v: f"{v:,.2f}")
            disp["fx_to_sgd"] = disp["fx_to_sgd"].apply(lambda v: f"{v:.6f}")
            st.dataframe(disp, width='stretch', hide_index=True)
            tot_sgd = cash_df["projected_sgd"].sum()
            st.caption(f"Projected cash total: S$ {tot_sgd:,.2f}")

        st.divider()

        # ---- Manual cash holdings (override) ----
        st.subheader("Current cash holdings (manual entry)")
        st.caption("Enter the cash you hold today, per currency. These are stored as "
                   "external_cash_balances and added to NAV alongside the live API cash. "
                   "Use this if your live API cash is missing balances (e.g. cash in the "
                   "Moomoo Cash sub-account that accinfo_query doesn't return).")
        with st.form("overview_cash_form"):
            cash_date = st.date_input("As of date", value=date.today(), key="ov_cash_date")
            cols = st.columns(4)
            sgd_cash = cols[0].number_input("SGD cash", min_value=0.0, step=10.0, value=0.0, format="%.2f")
            usd_cash = cols[1].number_input("USD cash", min_value=0.0, step=10.0, value=0.0, format="%.2f")
            hkd_cash = cols[2].number_input("HKD cash", min_value=0.0, step=10.0, value=0.0, format="%.2f")
            jpy_cash = cols[3].number_input("JPY cash", min_value=0.0, step=10.0, value=0.0, format="%.2f")
            cash_source = st.text_input("Source / note", value="manual entry")
            cash_submit = st.form_submit_button("Save cash snapshot")
            if cash_submit:
                with Session() as session:
                    for ccy, amt in [("SGD", sgd_cash), ("USD", usd_cash),
                                      ("HKD", hkd_cash), ("JPY", jpy_cash)]:
                        if amt > 0:
                            session.add(ExternalCashBalance(
                                ccy=ccy, amount=amt, as_of_date=cash_date,
                                source=cash_source, memo="",
                                logged_at=datetime.utcnow(),
                            ))
                    session.commit()
                refresh_caches()
                st.success(f"Saved cash snapshot for {cash_date}. Refresh the page to see new NAV.")

        # Show current external-cash totals per ccy
        with Session() as session:
            ext_rows = (session.query(ExternalCashBalance)
                              .order_by(ExternalCashBalance.ccy,
                                        ExternalCashBalance.as_of_date.desc()).all())
            if ext_rows:
                shown = set()
                latest_per_ccy = []
                for r in ext_rows:
                    if r.ccy in shown:
                        continue
                    shown.add(r.ccy)
                    latest_per_ccy.append({
                        "ccy": r.ccy, "as_of": r.as_of_date,
                        "amount": r.amount, "source": r.source or "",
                    })
                if latest_per_ccy:
                    st.write("**Latest cash entries used in NAV:**")
                    df_cash = pd.DataFrame(latest_per_ccy)
                    df_cash["amount"] = df_cash["amount"].apply(lambda v: f"{v:,.2f}")
                    st.dataframe(df_cash, width='stretch', hide_index=True)

        st.divider()

        # Projected positions — with LIVE prices and unrealised P&L
        st.subheader("Positions · anchor → live mark-to-market")
        st.caption("Live prices come from OpenAPI when available. Funds fall back to "
                   "anchor-statement price (OpenAPI doesn't return funds). Cost basis = "
                   "anchor position value + every Buy/Sell since anchor matched FIFO.")
        pos_df = pd.DataFrame(proj["projected_positions"])
        if not pos_df.empty:
            keep = ["symbol", "ccy", "qty", "qty_delta",
                    "anchor_price", "live_price", "used_live_price",
                    "cost_basis_local", "mv_local", "fx_to_sgd",
                    "mv_sgd", "unrealised_pnl_local", "unrealised_pnl_sgd"]
            keep = [c for c in keep if c in pos_df.columns]
            view = pos_df[keep].copy()
            st.dataframe(
                view, width='stretch', hide_index=True,
                column_config={
                    "symbol":               st.column_config.TextColumn("Symbol", width="small"),
                    "ccy":                  st.column_config.TextColumn("Ccy",    width="small"),
                    "qty":                  st.column_config.NumberColumn("Quantity",          format="%.4f"),
                    "qty_delta":            st.column_config.NumberColumn("Qty Δ vs anchor",   format="%+.4f"),
                    "anchor_price":         st.column_config.NumberColumn("Price @ anchor",    format="%.4f"),
                    "live_price":           st.column_config.NumberColumn("Live price",        format="%.4f"),
                    "used_live_price":      st.column_config.CheckboxColumn("Live?"),
                    "cost_basis_local":     st.column_config.NumberColumn("Cost basis (local)",     format="%.2f"),
                    "mv_local":             st.column_config.NumberColumn("Market value (local)",   format="%.2f"),
                    "fx_to_sgd":            st.column_config.NumberColumn("FX → SGD",              format="%.6f"),
                    "mv_sgd":               st.column_config.NumberColumn("Market value (SGD)",     format="%.2f"),
                    "unrealised_pnl_local": st.column_config.NumberColumn("Unrealised P&L (local)", format="%+.2f"),
                    "unrealised_pnl_sgd":   st.column_config.NumberColumn("Unrealised P&L (SGD)",   format="%+.2f"),
                }
            )
            tot_pos_sgd = pos_df["mv_sgd"].sum()
            tot_unreal = pos_df["unrealised_pnl_sgd"].sum() if "unrealised_pnl_sgd" in pos_df.columns else 0
            st.markdown(
                f"<div style='padding:0.6rem 0;color:#94a3b8;'>"
                f"Market value <b style='color:#e2e8f0'>S$ {tot_pos_sgd:,.2f}</b>"
                f"&nbsp;·&nbsp;Mark-to-market P&L <b style='color:{'#4ade80' if tot_unreal>=0 else '#f87171'}'>"
                f"S$ {tot_unreal:+,.2f}</b>"
                f"</div>",
                unsafe_allow_html=True,
            )

        st.divider()

        # Live drift check
        st.subheader("Sanity check: projected vs live API")
        vs_live = load_projection_vs_live()
        if vs_live is not None and not vs_live.empty:
            disp = vs_live.copy()
            for c in ["projected_cash", "live_cash", "drift"]:
                disp[c] = disp[c].apply(lambda v: f"{v:,.2f}")
            st.dataframe(disp, width='stretch', hide_index=True)
            st.caption("If drift is non-zero on any currency, there's a deposit/withdrawal/fee "
                       "we haven't logged. Use the Log Event tab to add it.")

    st.divider()

    _nav = d["current_nav"]
    col1, col2, col3 = st.columns(3)
    col1.metric("Live snapshot", str(_nav["when"])[:19] if _nav.get("when") else "—")
    col2.metric("Last statement", str(d["recon"]["last_statement"]) if d["recon"]["last_statement"] else "—")
    col3.metric("Concentration (HHI)", f"{d['hhi']:.0f}", help="0-10,000. Above 2,500 = highly concentrated.")

    st.divider()

    # ---- P&L Breakdown ----
    st.subheader("P&L Breakdown (SGD, since inception)")
    st.caption("Decomposes Total P&L = Current NAV − Net Deposits into named components. "
               "The residual line should be small if all data is captured correctly.")
    br = load_pnl_breakdown()
    bdown_df = pd.DataFrame([
        ("Equity realised P&L",   br["equity_realised"]),
        ("Equity unrealised P&L", br["equity_unrealised"]),
        ("Fund P&L (approx)",     br["fund_pnl"]),
        ("Dividends received",    br["dividends"]),
        ("Trading fees",          br["trading_fees"]),
        ("Residual (FX on cash / unattributed)", br["residual"]),
        ("─── Total explained ───",       br["total_explained"]),
        ("Total actual (NAV − Deposits)", br["total_actual"]),
    ], columns=["Component", "SGD"])
    bdown_df["SGD"] = bdown_df["SGD"].apply(lambda v: f"{v:+,.2f}")
    st.dataframe(bdown_df, width='stretch', hide_index=True)
    if br["external_cash_sgd"]:
        st.caption(f"Includes S$ {br['external_cash_sgd']:,.2f} of external cash "
                   "you've logged. Manage in Log Event → External Cash.")

    st.divider()
    st.subheader("Data ingested")
    r = d["recon"]
    st.write(
        f"- **{r['n_statements']}** monthly statements\n"
        f"- **{r['n_cash_events']}** cash events from statements\n"
        f"- **{r['n_trades']}** trade fills from CSV\n"
        f"- **{r.get('n_fund_orders', 0)}** fund orders from CSV\n"
        f"- **{r['n_manual_pending']}** pending manual events"
    )

# ===========================================================================
# Since Anchor — every trade, fund order, and manual event after the anchor
# ===========================================================================

with tabs[1]:
    anc = load_anchor()
    if not anc.get("anchor_date"):
        st.info("No statement ingested yet.")
    else:
        st.caption(f"Activity since the anchor date **{anc['anchor_date']}** "
                   f"(`{anc['anchor_filename']}`).")

        # Three summary cards
        proj = load_projection()
        c1, c2, c3 = st.columns(3)
        c1.metric("Deposits / withdrawals (SGD)",
                  f"S$ {proj['deposits_since_anchor_sgd']:+,.2f}")
        c2.metric("Projected NAV delta",
                  f"S$ {proj['projected_nav_sgd'] - anc['nav_sgd']:+,.2f}")
        c3.metric("P&L since anchor",
                  f"S$ {proj['since_anchor_pnl_sgd']:+,.2f}")

        st.divider()

        st.subheader("Trades since anchor")
        tr = load_trades_since_anchor(anc["anchor_date"])
        if tr is None or tr.empty:
            st.info("No trades since anchor.")
        else:
            disp = tr.copy()
            for c in ["qty", "price", "fees", "net_cash_impact"]:
                disp[c] = disp[c].apply(lambda v: f"{v:,.4f}" if c in ("qty",) else f"{v:+,.2f}")
            st.dataframe(disp, width='stretch', hide_index=True)
            st.caption(f"{len(tr)} fills since anchor")

            # Per-ccy net trade cash impact
            by_ccy = tr.groupby("ccy")["net_cash_impact"].sum().reset_index()
            by_ccy.columns = ["ccy", "net_cash_impact_local"]
            by_ccy["net_cash_impact_local"] = by_ccy["net_cash_impact_local"].apply(lambda v: f"{v:+,.2f}")
            st.write("**Net trade cash impact per ccy** (negative = bought more than sold):")
            st.dataframe(by_ccy, width='stretch', hide_index=True)

        st.divider()

        st.subheader("Fund orders since anchor")
        fnd = load_funds_since_anchor(anc["anchor_date"])
        if fnd is None or fnd.empty:
            st.info("No fund orders since anchor.")
        else:
            disp = fnd.copy()
            disp["amount"] = disp["amount"].apply(lambda v: f"{v:,.2f}" if pd.notna(v) else "—")
            disp["units"] = disp["units"].apply(lambda v: f"{v:,.4f}" if pd.notna(v) else "—")
            st.dataframe(disp, width='stretch', hide_index=True)

        st.divider()

        st.subheader("Manual cash events since anchor")
        mn = load_manuals_since_anchor(anc["anchor_date"])
        if mn is None or mn.empty:
            st.info("No manual events since anchor. (Use Log Event tab to add.)")
        else:
            disp = mn.copy()
            disp["amount"] = disp["amount"].apply(lambda v: f"{v:+,.2f}")
            st.dataframe(disp, width='stretch', hide_index=True)


# ===========================================================================
# NAV History
# ===========================================================================

with tabs[2]:
    nav_df = load_nav_series()
    if nav_df.empty:
        st.info("No NAV snapshots yet. Run `python3 run_ingest.py` to ingest statements.")
    else:
        st.subheader("Month-end NAV (SGD)")
        chart_df = nav_df.set_index("date")[["nav_sgd", "cash_sgd", "positions_sgd"]]
        st.line_chart(chart_df, height=400)

        st.subheader("Monthly net cash flows (SGD)")
        flows_df = load_monthly_flows()
        if not flows_df.empty:
            pivot = flows_df.pivot(index="month", columns="category", values="amount_sgd").fillna(0)
            st.bar_chart(pivot, height=300)
        else:
            st.info("No cash flows recorded yet.")

# ===========================================================================
# Statistics — performance analytics
# ===========================================================================

with tabs[3]:
    s = load_stats_block()

    if s["monthly"].empty or len(s["monthly"]) < 2:
        st.info("Need at least 2 months of statements to compute performance stats. "
                "Run `python3 run_ingest.py` after dropping more monthly PDFs.")
    else:
        # Top row: key performance numbers
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("TWR (annualised)", f"{s['twr_pct']:+.2f}%",
                  help="Time-Weighted Return — compares periods evenly, "
                       "useful for benchmarking against indices.")
        c2.metric("IRR (money-weighted)", f"{s['irr_pct']:+.2f}%",
                  help="Internal Rate of Return — your actual experience "
                       "given the timing of deposits/withdrawals.")
        c3.metric("Volatility (annual)", f"{s['vol_pct']:.2f}%",
                  help="Annualised standard deviation of monthly returns.")
        c4.metric("Sharpe", f"{s['sharpe']:.2f}",
                  help="(TWR - 3.5% risk-free) / vol. >1 is good, >2 is great.")
        c5.metric("Max Drawdown", f"{s['max_dd_pct']:.2f}%",
                  help="Worst peak-to-trough decline on month-end basis.")

        st.divider()

        # Cumulative return chart with benchmark
        st.subheader("Cumulative return vs benchmark")
        col_a, col_b = st.columns([1, 4])
        benchmark = col_a.selectbox(
            "Benchmark",
            options=["SPY", "ES3.SI", "QQQ", "ACWI", "(none)"],
            help="SPY = S&P 500. ES3.SI = SPDR STI ETF. "
                 "QQQ = Nasdaq 100. ACWI = MSCI All-Country World.",
        )
        cr = s["cum_return"].copy()
        cr = cr.rename(columns={"cum_return_pct": "Portfolio"})
        if benchmark != "(none)" and not cr.empty:
            start = cr["month"].min() + "-01"
            end = cr["month"].max() + "-28"
            bench = load_benchmark(benchmark, start, end)
            if not bench.empty:
                bench = bench.rename(columns={"cum_return_pct": benchmark})[["month", benchmark]]
                cr = cr.merge(bench, on="month", how="outer").sort_values("month")
        chart_df = cr.set_index("month")
        st.line_chart(chart_df, height=350)

        # Drawdown chart
        st.subheader("Drawdown from peak")
        dd = s["drawdown"].set_index("month")["drawdown_pct"]
        st.area_chart(dd, height=250, color="#d62728")

        st.divider()

        # P&L attribution per position (security vs FX)
        st.subheader("Unrealised P&L attribution — security vs FX (SGD)")
        st.caption("For each open position: security_pnl = price move in trade ccy × avg buy FX. "
                   "fx_pnl = current market value × (current FX − avg buy FX). They sum to total.")
        attr = load_attribution()
        if attr is not None and not attr.empty:
            disp = attr.copy()
            for c in ["cost_basis_local", "current_value_local"]:
                disp[c] = disp[c].apply(lambda v: f"{v:,.2f}" if pd.notna(v) else "—")
            for c in ["avg_buy_fx", "current_fx"]:
                disp[c] = disp[c].apply(lambda v: f"{v:.6f}" if pd.notna(v) else "—")
            for c in ["security_pnl_sgd", "fx_pnl_sgd", "total_pnl_sgd"]:
                disp[c] = disp[c].apply(lambda v: f"{v:+,.2f}" if pd.notna(v) else "—")
            st.dataframe(disp, width='stretch', hide_index=True)
        else:
            st.info("Need live position snapshot + trade history. Run `python3 run_phase2.py`.")

        st.divider()

        # Closed lots / realised P&L
        st.subheader("Closed positions (realised P&L)")
        cl = load_closed_lots()
        if cl is not None and not cl.empty:
            disp = cl.copy()
            for c in ["cost", "sell_price"]:
                disp[c] = disp[c].apply(lambda v: f"{v:,.4f}")
            for c in ["pnl_local"]:
                disp[c] = disp[c].apply(lambda v: f"{v:+,.2f}")
            disp["pnl_sgd"] = disp["pnl_sgd"].apply(lambda v: f"{v:+,.2f}")
            st.dataframe(disp, width='stretch', hide_index=True)
            st.caption(f"Total realised P&L: S$ {cl['pnl_sgd'].sum():+,.2f} across {len(cl)} closed lots")
        else:
            st.info("No closed positions yet.")

        st.divider()

        # Currency attribution rollup (aggregated by ccy)
        st.subheader("Current exposure by currency")
        ca = s["ccy_attribution"]
        if not ca.empty:
            disp = ca.copy()
            for c in ["mv_local", "mv_sgd", "pl_security_local", "pl_security_sgd"]:
                if c in disp.columns:
                    disp[c] = disp[c].apply(lambda v: f"{v:,.2f}")
            st.dataframe(disp, width='stretch', hide_index=True)

        st.divider()

        # Per-currency FX history
        st.subheader("FX rate history (per currency you hold, to SGD)")
        st.caption("Use this to see how much of each position's gain is really currency movement.")
        ccys_held = tuple(sorted({c for c in (s["ccy_attribution"]["ccy"].tolist() if not s["ccy_attribution"].empty else []) if c and c != "SGD"}))
        if ccys_held:
            fx_df = load_fx_history(ccys_held)
            if not fx_df.empty:
                st.line_chart(fx_df, height=300)
            else:
                st.info("No FX history yet. Run `python3 run_phase2.py` to backfill.")
        else:
            st.info("All positions are SGD — no FX chart needed.")

        st.divider()

        # Monthly return table
        st.subheader("Monthly returns")
        mr = s["monthly"].copy()
        mr["return_pct"] = mr["return"].apply(lambda v: f"{v*100:+.2f}%")
        mr["nav_sgd"] = mr["nav_sgd"].apply(lambda v: f"{v:,.2f}")
        mr["flow_sgd"] = mr["flow_sgd"].apply(lambda v: f"{v:+,.2f}")
        st.dataframe(
            mr[["month", "nav_sgd", "flow_sgd", "return_pct"]],
            width='stretch', hide_index=True,
        )


# ===========================================================================
# Positions
# ===========================================================================

with tabs[4]:
    pos_df = load_positions()
    if pos_df.empty:
        st.info("No live position snapshot yet. Run `python3 run_phase2.py`.")
    else:
        st.subheader("Current positions (live)")
        # Format for display
        disp = pos_df.copy()
        for col in ["qty", "cost_price", "price"]:
            disp[col] = disp[col].apply(lambda v: f"{v:,.4f}" if v else "—")
        for col in ["mv_local", "mv_sgd", "pl_local", "pl_sgd"]:
            disp[col] = disp[col].apply(lambda v: f"{v:,.2f}")
        disp["weight_pct"] = disp["weight_pct"].apply(lambda v: f"{v:.1f}%")
        disp["fx_to_sgd"] = disp["fx_to_sgd"].apply(lambda v: f"{v:.6f}")
        st.dataframe(disp, width='stretch', hide_index=True)

        st.divider()
        st.subheader("Allocation by currency")
        ccy_df = pos_df.groupby("ccy", as_index=False)["mv_sgd"].sum()
        ccy_df["weight_pct"] = ccy_df["mv_sgd"] / ccy_df["mv_sgd"].sum() * 100
        st.bar_chart(ccy_df.set_index("ccy")["mv_sgd"], height=250)

# ===========================================================================
# Capital Flows
# ===========================================================================

with tabs[5]:
    flows_df = load_cash_events()
    if flows_df.empty:
        st.info("No cash events yet.")
    else:
        col1, col2 = st.columns([1, 1])
        ccy_filter = col1.multiselect("Currency", sorted(flows_df["ccy"].unique()),
                                       default=list(sorted(flows_df["ccy"].unique())))
        type_filter = col2.multiselect("Type", sorted(flows_df["type"].unique()),
                                        default=list(sorted(flows_df["type"].unique())))
        view = flows_df[
            flows_df["ccy"].isin(ccy_filter) &
            flows_df["type"].isin(type_filter)
        ].copy()
        view["amount"] = view["amount"].apply(lambda v: f"{v:+,.2f}")
        st.dataframe(view, width='stretch', hide_index=True)

# ===========================================================================
# Trades
# ===========================================================================

with tabs[6]:
    trades_df = load_trades()
    if trades_df.empty:
        st.info("No trades ingested yet. Run `python3 run_phase2.py`.")
    else:
        col1, col2, col3 = st.columns(3)
        sym = col1.multiselect("Symbol", sorted(trades_df["symbol"].unique()))
        side = col2.multiselect("Side", sorted(trades_df["side"].unique()))
        ccy = col3.multiselect("Currency", sorted(trades_df["ccy"].unique()))
        view = trades_df.copy()
        if sym:
            view = view[view["symbol"].isin(sym)]
        if side:
            view = view[view["side"].isin(side)]
        if ccy:
            view = view[view["ccy"].isin(ccy)]
        for col in ["qty"]:
            view[col] = view[col].apply(lambda v: f"{v:,.2f}")
        for col in ["price", "fees", "net_cash"]:
            view[col] = view[col].apply(lambda v: f"{v:,.2f}")
        st.dataframe(view, width='stretch', hide_index=True)
        st.caption(f"{len(view)} fills shown")

# ===========================================================================
# Funds — fund orders + per-fund aggregate P&L
# ===========================================================================

with tabs[7]:
    st.subheader("Per-fund summary (Completed orders only)")
    fp = load_fund_pnl()
    if fp.empty:
        st.info("No fund orders ingested yet. Drop a `fund accounts.csv` into "
                "`statements/raw/` and run `python3 run_phase2.py`.")
    else:
        disp = fp.copy()
        for c in ["subscribed_sgd", "redeemed_sgd", "net_invested_sgd"]:
            disp[c] = disp[c].apply(lambda v: f"{v:,.2f}" if pd.notna(v) else "—")
        for c in ["units_subscribed", "units_redeemed", "units_remaining_est"]:
            disp[c] = disp[c].apply(lambda v: f"{v:,.4f}" if pd.notna(v) else "—")
        st.dataframe(disp, width='stretch', hide_index=True)
        total_net = fp["net_invested_sgd"].sum()
        st.caption(
            f"Total net invested across all funds (subscribed − redeemed, SGD): "
            f"S$ {total_net:,.2f}"
        )

    st.divider()

    st.subheader("All fund orders (raw)")
    fo = load_fund_orders()
    if fo.empty:
        st.info("No fund orders ingested.")
    else:
        col1, col2, col3 = st.columns(3)
        type_filter = col1.multiselect("Order type", sorted(fo["type"].unique()),
                                        default=list(sorted(fo["type"].unique())))
        status_filter = col2.multiselect("Status", sorted(fo["status"].unique()),
                                          default=["Completed"])
        ccy_filter = col3.multiselect("Currency", sorted(fo["ccy"].unique()),
                                       default=list(sorted(fo["ccy"].unique())))
        view = fo[
            fo["type"].isin(type_filter)
            & fo["status"].isin(status_filter)
            & fo["ccy"].isin(ccy_filter)
        ].copy()
        view["amount"] = view["amount"].apply(
            lambda v: f"{v:,.2f}" if pd.notna(v) else "—")
        view["units"] = view["units"].apply(
            lambda v: f"{v:,.4f}" if pd.notna(v) else "—")
        st.dataframe(view, width='stretch', hide_index=True)
        st.caption(f"{len(view)} fund orders shown")


# ===========================================================================
# Rebalance — current vs target weights, suggested trades
# ===========================================================================

with tabs[8]:
    st.subheader("Rebalancing helper")
    st.caption("Set a target weight per symbol (in %). The table below shows your current weight, "
               "delta vs target, and suggested SGD trade to get you back in line. "
               "Targets need not sum to 100% — un-targeted positions are simply ignored on the delta side.")

    # Targets-management form
    with st.expander("Set / update target weights", expanded=False):
        with st.form("target_form"):
            tsym = st.text_input("Symbol (e.g. RKLB, O39, 02802)")
            ttgt = st.number_input("Target weight (%)", min_value=0.0, max_value=100.0,
                                    step=0.5, value=5.0)
            tnotes = st.text_input("Notes (optional)")
            tsub = st.form_submit_button("Save target")
            if tsub and tsym.strip():
                with Session() as session:
                    existing = session.query(TargetWeight).filter_by(symbol=tsym.strip().upper()).one_or_none()
                    if existing:
                        existing.target_weight_pct = ttgt
                        existing.notes = tnotes
                    else:
                        session.add(TargetWeight(
                            symbol=tsym.strip().upper(),
                            target_weight_pct=ttgt, notes=tnotes,
                        ))
                    session.commit()
                refresh_caches()
                st.success(f"Target for {tsym.strip().upper()} set to {ttgt}%")

        with Session() as session:
            targets_now = session.query(TargetWeight).all()
            if targets_now:
                st.write("**Current targets:**")
                for t in targets_now:
                    cc1, cc2 = st.columns([5, 1])
                    cc1.write(f"`#{t.id}`  **{t.symbol}** → {t.target_weight_pct:.1f}%  {('— ' + t.notes) if t.notes else ''}")
                    if cc2.button("Delete", key=f"deltgt_{t.id}"):
                        with Session() as s2:
                            s2.query(TargetWeight).filter_by(id=t.id).delete()
                            s2.commit()
                        refresh_caches()
                        st.rerun()

    rb = load_rebalance()
    if rb is None or rb.empty:
        st.info("No positions yet, or no live snapshot. Run `python3 run_phase2.py`.")
    else:
        disp = rb.copy()
        for c in ["current_pct", "target_pct", "delta_pct"]:
            disp[c] = disp[c].apply(lambda v: f"{v:.2f}%")
        for c in ["current_value_sgd", "target_value_sgd", "suggested_trade_sgd"]:
            disp[c] = disp[c].apply(lambda v: f"{v:+,.2f}")
        st.dataframe(disp, width='stretch', hide_index=True)
        st.caption("Positive suggested_trade_sgd = BUY more; negative = SELL.")


# ===========================================================================
# Log Event (form)
# ===========================================================================

with tabs[9]:
    # ---- External cash balance management ----
    st.subheader("External cash holdings (cash NOT visible via OpenAPI)")
    st.caption("Track cash you have outside the Moomoo trading-account API view. "
               "Examples: SGD in your Moomoo Cash sub-account, money in a separate "
               "broker, cash you mentally consider part of your portfolio. The LATEST "
               "balance per currency is added to your NAV and P&L.")
    with st.form("ext_cash_form"):
        col1, col2, col3 = st.columns(3)
        ext_ccy = col1.selectbox("Currency", ["SGD", "USD", "HKD", "JPY", "AUD"], key="ext_ccy")
        ext_amt = col2.number_input("Amount (positive)", min_value=0.0, step=10.0, value=0.0, format="%.2f")
        ext_date = col3.date_input("As of date", value=date.today(), key="ext_date")
        ext_source = st.text_input("Source / location", value="", placeholder="e.g. Moomoo Cash sub-account, DBS savings")
        ext_memo = st.text_input("Memo (optional)", key="ext_memo")
        ext_sub = st.form_submit_button("Save balance")
        if ext_sub and ext_amt > 0:
            with Session() as session:
                session.add(ExternalCashBalance(
                    ccy=ext_ccy, amount=ext_amt, as_of_date=ext_date,
                    source=ext_source, memo=ext_memo,
                    logged_at=datetime.utcnow(),
                ))
                session.commit()
            refresh_caches()
            st.success(f"Logged: {ext_date} {ext_ccy} {ext_amt:,.2f} from {ext_source or 'external'}")

    with Session() as session:
        ext_rows = (session.query(ExternalCashBalance)
                          .order_by(ExternalCashBalance.ccy,
                                    ExternalCashBalance.as_of_date.desc()).all())
        if ext_rows:
            st.write("**Recorded balances** (the LATEST per currency is what NAV uses):")
            for r in ext_rows:
                c1, c2 = st.columns([5, 1])
                c1.write(f"`#{r.id}`  {r.as_of_date}  **{r.ccy} {r.amount:,.2f}** "
                         f"— {r.source or '?'}  {(' · ' + r.memo) if r.memo else ''}")
                if c2.button("Delete", key=f"delext_{r.id}"):
                    with Session() as s2:
                        s2.query(ExternalCashBalance).filter_by(id=r.id).delete()
                        s2.commit()
                    refresh_caches()
                    st.rerun()

    st.divider()
    st.subheader("Log a deposit, withdrawal, or currency conversion")
    st.caption("These are manual entries for events that haven't yet appeared in a statement. "
               "When the next statement arrives and the matching event is parsed, you should delete the manual record.")

    mode = st.radio("Event type",
                    ["SGD deposit", "SGD withdrawal", "Currency conversion"],
                    horizontal=True)

    if mode in ("SGD deposit", "SGD withdrawal"):
        with st.form("flow_form"):
            evt_date = st.date_input("Date", value=date.today())
            amt = st.number_input("Amount (SGD)", min_value=0.0, step=10.0, value=100.0, format="%.2f")
            memo = st.text_input("Memo (e.g. 'PayNow', 'bank transfer')")
            submitted = st.form_submit_button("Log")
            if submitted:
                signed = amt if mode == "SGD deposit" else -amt
                with Session() as session:
                    session.add(ManualCashEvent(
                        event_date=evt_date, ccy="SGD", amount=signed,
                        memo=memo, logged_at=datetime.utcnow(),
                    ))
                    session.commit()
                refresh_caches()
                st.success(f"Logged: {evt_date}  SGD {signed:+,.2f}  '{memo}'")

    else:  # Currency conversion
        with st.form("fx_form"):
            evt_date = st.date_input("Date", value=date.today())
            col1, col2 = st.columns(2)
            from_ccy = col1.selectbox("From currency", ["USD", "HKD", "JPY", "SGD"])
            from_amt = col1.number_input(f"From amount ({from_ccy})", min_value=0.0,
                                          step=10.0, value=0.0, format="%.4f")
            to_ccy = col2.selectbox("To currency", ["SGD", "USD", "HKD", "JPY"])
            to_amt = col2.number_input(f"To amount ({to_ccy})", min_value=0.0,
                                        step=10.0, value=0.0, format="%.4f")
            memo = st.text_input("Memo", value=f"FX {from_ccy}->{to_ccy}")
            submitted = st.form_submit_button("Log conversion")
            if submitted and from_amt > 0 and to_amt > 0:
                rate = to_amt / from_amt
                with Session() as session:
                    session.add(ManualCashEvent(
                        event_date=evt_date, ccy=from_ccy, amount=-from_amt,
                        memo=f"{memo} (out)", logged_at=datetime.utcnow(),
                    ))
                    session.add(ManualCashEvent(
                        event_date=evt_date, ccy=to_ccy, amount=+to_amt,
                        memo=f"{memo} (in, rate={rate:.6f})",
                        logged_at=datetime.utcnow(),
                    ))
                    session.commit()
                refresh_caches()
                st.success(f"Logged FX: {evt_date}  {from_ccy} -{from_amt:,.2f}  ->  "
                           f"{to_ccy} +{to_amt:,.2f}  (rate {rate:.6f})")

    st.divider()
    st.subheader("Pending manual events")
    with Session() as session:
        pending = session.query(ManualCashEvent).filter(
            ManualCashEvent.confirmed_at.is_(None)
        ).order_by(ManualCashEvent.event_date.desc()).all()
        if not pending:
            st.info("Nothing pending.")
        else:
            for m in pending:
                col1, col2 = st.columns([5, 1])
                col1.write(f"`#{m.id}`  {m.event_date}  **{m.ccy}** {m.amount:+,.2f}  — _{m.memo}_")
                if col2.button("Delete", key=f"del_{m.id}"):
                    with Session() as s2:
                        s2.query(ManualCashEvent).filter_by(id=m.id).delete()
                        s2.commit()
                    refresh_caches()
                    st.rerun()
