# Formula Reference — every calculation in the dashboard

This document maps every number you see on the dashboard back to:

1. The exact file and function that computes it
2. The math notation
3. A worked example using your own data so you can verify by hand

Open VS Code, `cmd-click` any function name below to jump to its definition.
If a formula looks wrong, edit the file directly — the dashboard re-reads on
every page refresh.

---

## 0 · Conventions

- **`rate_to_sgd`** for any ccy = how many SGD you get for 1 unit of that ccy.
  - USD ≈ 1.27 (1 USD = 1.27 SGD), so `rate_to_sgd[USD] = 1.27`
  - HKD ≈ 0.165, JPY ≈ 0.0081, SGD = 1.0
- **To convert from any ccy to SGD:** `amount_local × rate_to_sgd`. Always
  multiply; never divide. The inversion (if any) happens once when we *fetch*
  the FX rate, not when we *use* it.
- Sign convention for cash events:
  - Deposit (Cash In Out from Moomoo) = positive amount = money in
  - Withdrawal = negative amount = money out
- Sign convention for IRR (investor POV):
  - Deposit = negative cash flow (money out of pocket)
  - Withdrawal = positive cash flow
  - Terminal NAV = positive cash flow

---

## 1 · FX rate lookup

**File:** `src/stats.py`
**Function:** `fx_at(session, ccy, when)`

```python
if ccy == "SGD":
    return 1.0
row = (session.query(FxRate)
              .filter(FxRate.ccy == ccy, FxRate.rate_date <= when)
              .order_by(FxRate.rate_date.desc())
              .first())
return row.rate_to_sgd if row else 1.0
```

Notes:
- Always uses the most recent rate on or before `when`. Forward-fills weekends.
- Falls back to `1.0` silently if no rate found. Run `python3 -m src.fx` to
  refresh from yfinance.

**Where FX rates come from:** `src/fx.py`, function `backfill()`. yfinance
ticker conventions:
- `SGD=X` returns SGD-per-USD (direct), so for USD we store as-is
- `SGDHKD=X` returns HKD-per-SGD (inverted), so for HKD we store `1/rate`
- `SGDJPY=X` returns JPY-per-SGD (inverted), so for JPY we store `1/rate`

---

## 2 · Realised P&L (closed equity lots)

**Files:** `src/lots.py`

### 2a · Per-fill FIFO matching
**Function:** `_lots_for_symbol(session, symbol, trades)`

For each Buy trade on a symbol, open a new lot:
```
fee_per_share    = fees_total / qty
cost_per_share   = fill_price + fee_per_share
open_lot         = {qty, cost_per_share}
```

For each Sell trade, close oldest lots first (FIFO):
```
net_proceeds_per_share = fill_price - fee_per_share
for each open_lot (oldest first):
    shares = min(open_lot.qty, remaining_to_sell)
    pnl_local = (net_proceeds_per_share - open_lot.cost_per_share) × shares
    pnl_sgd   = pnl_local × fx_at(session, trade.ccy, trade.fill_time)
```

### 2b · Total realised P&L
**Function:** `total_realised_pnl_sgd(session)`

```
total = Σ over all closed_lots: pnl_sgd
```

### Worked example — RKLB sale on 2026-05-08 (32 shares @ $96.50)

You bought 32 RKLB shares originally for less; assume avg cost was $72.95 per
share (from the 2026-04-14 fill at 72.95). Fees on that buy: $1.12 → fee_per_share = $0.035.

```
cost_per_share = 72.95 + 0.035                = 72.985
sell at 2026-05-08: 32 shares @ 96.50
fees on sell: 1.26                            → fee_per_share = 0.039
net_proceeds  = 96.50 - 0.039                = 96.461
pnl_local     = (96.461 - 72.985) × 32       = 751.2 USD
USDSGD rate on 2026-05-08 ≈ 1.27
pnl_sgd       = 751.2 × 1.27                 ≈ +954.0 SGD
```

If the dashboard's row for this sale shows ≈ +954 SGD → realised P&L
calculation is correct.

---

## 3 · Unrealised P&L per current position (mark-to-market)

**File:** `src/anchor.py`
**Function:** `projected_current_state(session)`

For each currently-held position:

```
cost_basis_local  = (anchor_qty × anchor_price) + Σ (buys_since_anchor cost) − Σ (sells_since_anchor cost basis)
                    (FIFO matched — see _cost_basis_for_position)
live_price        = live_position_snapshots.nominal_price (or anchor_price if API missing)
mv_local          = qty × live_price
fx_to_sgd         = fx_at(session, ccy, now)
mv_sgd            = mv_local × fx_to_sgd

unrealised_pnl_local = mv_local - cost_basis_local
unrealised_pnl_sgd   = unrealised_pnl_local × fx_to_sgd
```

### Worked example — your TSLA position

April statement showed: 3 TSLA shares at $381.63 closing.
Then on 2026-05-11 you sold all 3 for $421.04 (realised gain).
You currently hold 0 TSLA → not in this table.

### Worked example — a USD position you still hold (RKLB)

April statement: 32 shares @ 82.51 closing.
Since: sold 32 on 2026-05-08, bought 1 (1 share) — net qty change −31.

If your current qty is `n` and live price is `P`:
```
cost_basis_local  = FIFO-derived total cost in USD for the n shares you have
mv_local          = n × P
unrealised_pnl_local = mv_local - cost_basis_local           (USD)
unrealised_pnl_sgd   = unrealised_pnl_local × 1.27           (SGD)
```

---

## 4 · NAV (Net Asset Value)

**File:** `src/anchor.py`, also `src/stats.py`

### 4a · NAV at anchor (statement-derived, fully reconciled)
**Function:** `anchor_state(session)["nav_sgd"]`

```
nav_sgd = Σ over each ccy: cash_balance_at_anchor × fx_rate_at_anchor
        + Σ over each position: market_value_sgd_at_anchor
```

For April 2026 this should equal **S$ 13,105.76** (page 1 of the statement).

### 4b · Projected NAV today
**Function:** `projected_current_state(session)["projected_nav_sgd"]`

```
projected_cash_sgd      = Σ over each ccy: (anchor_cash + delta_since_anchor) × current_FX
projected_positions_sgd = Σ over each open position: live_price × qty × current_FX
projected_nav_sgd       = projected_cash_sgd + projected_positions_sgd
```

`delta_since_anchor` = trade cash impacts + manual cash events since anchor.

---

## 5 · Total P&L (since inception)

**File:** `src/stats.py`
**Function:** `total_pnl_sgd(session)`

```python
return current_nav_sgd(session)["nav_sgd"] - total_net_deposits_sgd(session)
```

Plain English:
```
total_pnl = (everything you own today, SGD) - (every dollar deposited - every dollar withdrawn)
```

### Worked example with your numbers

Approximate values:
- Current NAV: S$ ~16,000 (with funds + cash + live equities)
- Net deposits across all statements + manual events: S$ ~11,500

```
total_pnl ≈ 16,000 - 11,500 = +4,500 SGD
```

This number combines realised gains, unrealised gains, dividends, fees, and FX.
For the breakdown into named components, see section 6.

---

## 6 · P&L breakdown (decomposition into components)

**File:** `src/stats.py`
**Function:** `pnl_breakdown(session)`

```
equity_realised   = lots.total_realised_pnl_sgd(session)
equity_unrealised = Σ over open positions: unrealised_pnl_sgd
fund_pnl          = fund_current_value_sgd - fund_net_invested_sgd
dividends         = Σ over cash_events where event_type in ('Corporate Action', 'Fund Corporate Action'): amount × fx_at(event_time)
trading_fees      = Σ over trades: -(fees_total × fx_at(trade.fill_time))    (negative because it's a cost)
total_explained   = equity_realised + equity_unrealised + fund_pnl + dividends - trading_fees
residual          = total_actual - total_explained
total_actual      = current_nav_sgd - net_deposits   (= total_pnl above)
```

The residual captures: FX-on-cash, withholding tax on dividends, statement-only
fees not in the CSV, interest income, fund subscription/redemption fees, and
parsing gaps. Should be small (under a few hundred SGD) if all data is captured.

---

## 7 · Time-Weighted Return (annualised)

**File:** `src/stats.py`
**Function:** `time_weighted_return_annualized_pct(session)`

### 7a · Monthly returns (Modified Dietz)
**Function:** `monthly_returns(session)`

For each month after the first:
```
flow_in_month   = Σ Cash In Out events + Σ manual events that month, all in SGD
return_m        = (NAV_end - NAV_start - flow_in_month) / (NAV_start + 0.5 × flow_in_month)
```

The `+ 0.5 × flow_in_month` assumes flows happen mid-month on average.

### 7b · Annualisation

```
cumulative_compound = Π over months: (1 + return_m)
n_months            = count of monthly returns
annualised_pct      = (cumulative_compound ^ (12 / n_months) - 1) × 100
```

### Worked example

If you had +2% in Nov, -1% in Dec, +3% in Jan:
```
cumulative = 1.02 × 0.99 × 1.03 = 1.0399 → +3.99% over 3 months
annualised = 1.0399 ^ (12/3) - 1 = 1.1697 - 1 = +16.97%
```

---

## 8 · Money-Weighted IRR (XIRR)

**File:** `src/stats.py`
**Function:** `money_weighted_irr_pct(session)`

Sign convention from investor POV:
- Deposits to broker → NEGATIVE cash flow (money out of pocket)
- Withdrawals from broker → POSITIVE cash flow (money into pocket)
- Terminal NAV today → POSITIVE cash flow (theoretical liquidation value)

Solve for `r` such that the discounted sum of all cash flows = 0:
```
xnpv(r) = Σ over (date, amount) flows: amount / (1 + r)^((date - t0).days / 365)
```

Uses `scipy.optimize.brentq` to find the root in `[-99%, +1000%]`.

### How TWR vs IRR differ

- **TWR** treats every period equally. Good for comparing yourself against a
  benchmark (the index doesn't care when you deposited).
- **IRR** weights periods by how much money was at work. Captures your
  *lived experience* as an investor.

If you deposited heavily into a bull market, IRR > TWR. If you deposited
into a downturn, IRR < TWR.

---

## 9 · Sharpe ratio

**File:** `src/stats.py`
**Function:** `sharpe_ratio(session, rf_annual=0.035)`

```
twr_annual_decimal  = time_weighted_return_annualized_pct / 100
vol_annual_decimal  = volatility_annualized_pct / 100
sharpe              = (twr_annual_decimal - rf_annual) / vol_annual_decimal
```

`rf_annual` defaults to 3.5% (MAS 6-month T-bill). Edit the default at
`src/stats.py` `DEFAULT_RF_ANNUAL = 0.035`.

A Sharpe of 1.0 means you earned 1 unit of excess-over-riskfree per unit of
volatility. Above 1 = good, above 2 = excellent.

---

## 10 · Volatility (annualised)

**File:** `src/stats.py`
**Function:** `volatility_annualized_pct(session)`

```
monthly_returns   = monthly_returns(session)["return"][1:]   # drop first (no return)
annualised_pct    = monthly_returns.std(ddof=1) × √12 × 100
```

The √12 scales monthly std-dev to annual. Standard finance convention.

---

## 11 · Max drawdown

**File:** `src/stats.py`
**Function:** `max_drawdown_pct(session)`

```
cum_series        = Π monthly returns
running_peak      = cumulative maximum of cum_series
drawdown_at_t     = (cum_series[t] / running_peak[t]) - 1
max_drawdown_pct  = min(drawdown_at_t) × 100
```

Always negative. If max DD = -25%, you were 25% below your then-peak at the
worst point.

---

## 12 · Currency attribution

**File:** `src/lots.py`
**Function:** `unrealised_pnl_attribution(session)`

For each currently-held position, decompose SGD P&L into security move
vs FX move:

```
avg_buy_fx           = Σ (lot.qty × lot.cost_per_share × fx_at(buy_time)) / cost_basis_local
                       (weighted average buy-time FX, weighted by lot value)
pl_local             = current_value_local - cost_basis_local
security_pnl_sgd     = pl_local × avg_buy_fx
fx_pnl_sgd           = current_value_local × (current_fx - avg_buy_fx)
total_pnl_sgd        = security_pnl_sgd + fx_pnl_sgd
```

This answers "of my SGD gain on RKLB, how much was the stock vs how much was
USD/SGD moving against me?"

---

## 13 · Reconciliation (drift detection)

**File:** `src/reconcile.py`
**Function:** `reconcile()` and `compute_drift_summary(session)`

```
expected_cash_per_ccy  = anchor_statement_ending_cash_per_ccy
                       + Σ trades-since-anchor.net_cash_impact per ccy
                       + Σ manual_cash_events-since-anchor.amount per ccy

live_cash_per_ccy      = latest LiveCashSnapshot per ccy

drift_per_ccy          = live - expected
```

If `|drift| > TOLERANCE` (5 SGD), the per-ccy line is flagged
`DEPOSIT?` (drift positive) or `WITHDRAWAL?` (drift negative).

---

## 14 · How to manually verify a number from the dashboard

For ANY number you doubt:

1. Find the metric on the dashboard. Each card has a `?` help tooltip that
   says exactly which calculation.
2. Look up the corresponding formula in this document.
3. Open the source file in VS Code (`cmd-click` the function name).
4. Read the 5–30 lines of code that compute it.
5. Run this query to inspect raw inputs:

```bash
sqlite3 data/portfolio.db <<'SQL'
.headers on
.mode column
-- Replace with whatever table feeds the number you're checking
SELECT * FROM trades WHERE symbol='RKLB' ORDER BY fill_time;
SELECT * FROM cash_events WHERE event_type='Cash In Out' ORDER BY event_time;
SELECT * FROM nav_snapshots WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM nav_snapshots);
SQL
```

If the inputs are right but the output is wrong, the formula has a bug —
edit it in place and reload the dashboard.

---

## 15 · Most common sources of discrepancy

| Symptom | Likely cause | Fix |
|---|---|---|
| USD positions look ~62% of true value | `fx_rates` table has bad USD rates | `sqlite3 data/portfolio.db "DELETE FROM fx_rates WHERE source='yfinance';"` then `python3 -m src.fx` |
| Fund P&L looks off | Fund cost basis is approximate (uses net_invested vs current_value, doesn't FIFO units) | Acceptable for v1; precise per-lot fund tracking requires statement page 7 parsing |
| NAV doesn't match Moomoo app exactly | Live snapshot is stale, or external cash not entered | Run `python3 run_phase2.py` then enter cash on Overview tab |
| Drift > 5 SGD on a currency | Unlogged deposit/withdrawal/conversion | Log via Log Event tab or `python3 -m src.reconcile add-flow` |
| Realised P&L wildly different from gut | FX rates missing during the trade period | `python3 -m src.fx 2024-11-01` to backfill from inception |
