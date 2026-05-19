# Moomoo Portfolio Dashboard

A personal portfolio-tracking system for Moomoo SG accounts. Parses monthly
statements, trade exports, and fund-order exports into a SQLite database;
syncs live cash and positions via the Moomoo OpenAPI; reconciles everything
against statement-anchored ground truth; and renders it as a Streamlit
dashboard.

Built because Moomoo's own analytics didn't quite show what I wanted —
realised vs unrealised P&L per position, currency attribution, true
mark-to-market against cost basis, and a clean audit trail back to each
monthly statement.

## What it does

- **Parses Moomoo SG monthly statement PDFs** — extracts NAV, per-currency
  cash, every cash event (deposits, withdrawals, dividends, FX, fees),
  end-of-month positions including funds.
- **Ingests trade CSVs and fund-order CSVs** — handles multi-fill rollup,
  per-currency fee columns, FIFO matching for cost basis.
- **Pulls live cash + equity positions via the Moomoo OpenAPI**
  (`futu-api` SDK + OpenD gateway).
- **Backfills FX rates** from yfinance so SGD-equivalent calculations work
  on any historical date.
- **Reconciles** the statement-anchored expected state against the live API
  snapshot, flagging drift > S$5 in any currency.
- **Streamlit dashboard** with 10 tabs covering overview, since-anchor
  activity, NAV history, performance statistics (TWR, IRR, Sharpe, max
  drawdown), positions, capital flows, trades, funds, rebalancing, and
  manual event logging.
- **Automation**: a launchd job refreshes data every morning at 8am and
  triggers a macOS notification if reconciliation drift is detected.

## Architecture

```
       ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
       │ Monthly         │  │ Trade History   │  │ Fund Orders     │
       │ Statement PDFs  │  │ CSV             │  │ CSV             │
       └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
                │                    │                    │
                ▼                    ▼                    ▼
       ┌────────────────────────────────────────────────────────┐
       │  Ingestion (run_ingest.py / run_phase2.py)              │
       │  • parse_statement.py  • csv_trades.py  • fund_csv.py   │
       └────────────────────────┬───────────────────────────────┘
                                ▼
                       ┌────────────────┐         ┌─────────────────┐
                       │  SQLite DB     │ ◄──────│  Moomoo OpenD    │
                       │  portfolio.db  │         │  (live cash +    │
                       └────────┬───────┘         │   positions)     │
                                │                 └─────────────────┘
                                ▼
                       ┌─────────────────┐
                       │  Streamlit app  │
                       │  src/app.py     │
                       └─────────────────┘
```

## Anchor-and-delta model

Rather than trying to compute everything from raw events, the system:

1. Treats the **most recent monthly statement** as the authoritative anchor
   (reconciled by Moomoo, no estimates).
2. Tracks every trade, fund order, and manual cash event **since the anchor**.
3. Computes "projected current state" = anchor + delta, with positions
   marked-to-market using live OpenAPI prices.

This keeps the math reliable: statements are immutable ground truth; only
*since-anchor* activity needs careful tracking. When a new statement arrives,
the anchor advances and the delta resets.

## Getting started

### Prerequisites

- macOS (the launchd automation is Mac-specific; the core dashboard works
  on any OS)
- Python 3.11+
- A Moomoo SG account with OpenAPI access enabled
- The [OpenD desktop gateway](https://www.moomoo.com/download/OpenAPI)
  installed and logged in

### Install

```bash
git clone https://github.com/<your-username>/moomoo-portfolio.git
cd moomoo-portfolio
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set FUTU_TRD_PWD to your 6-digit Moomoo trading PIN
```

### First run

```bash
# Drop your monthly statement PDFs into statements/raw/
# (and your History CSV, and fund-orders CSV)

python3 run_ingest.py        # parse statements
python3 run_phase2.py        # CSV trades + FX + OpenAPI sync + reconcile
streamlit run src/app.py     # launch dashboard at http://localhost:8501
```

### Daily refresh (optional)

```bash
bash scripts/install_schedule.sh        # daily 8am refresh job
bash scripts/install_dashboard_service.sh   # always-on dashboard at boot
```

## File layout

```
.
├── src/
│   ├── db.py              # SQLAlchemy schema (statements, trades, funds, etc.)
│   ├── parse_statement.py # PDF parser
│   ├── csv_trades.py      # Trade History CSV parser
│   ├── fund_csv.py        # Fund orders CSV parser
│   ├── fx.py              # yfinance FX backfill
│   ├── openapi_sync.py    # Moomoo OpenAPI live snapshot
│   ├── reconcile.py       # Drift detection
│   ├── stats.py           # NAV / P&L / TWR / IRR / Sharpe / etc.
│   ├── lots.py            # FIFO cost-basis matching
│   ├── anchor.py          # Anchor-and-delta projection
│   ├── notify.py          # macOS notifications
│   └── app.py             # Streamlit dashboard
├── scripts/               # launchd plists + start/stop scripts
├── statements/raw/        # YOUR monthly statement PDFs + CSVs (gitignored)
├── data/                  # SQLite DB + logs (gitignored)
├── run_ingest.py          # parse all statements
├── run_phase2.py          # refresh trades + FX + live + reconcile
├── run_dashboard.sh       # phase 2 + launch dashboard
├── FORMULAS.md            # every calculation walked through
└── README.md
```

## Formula reference

Every calculation on the dashboard is documented in
[`FORMULAS.md`](FORMULAS.md) with file/function references and worked examples.

## Privacy

This repo contains **only the code**. Your actual data — statements,
trades, balances — lives in `data/portfolio.db` and `statements/raw/`,
both of which are gitignored. Nothing about your portfolio is committed.

When sharing or deploying this project, double-check `git status` shows
no statement files or `.db` files before pushing.

## License

MIT. See [LICENSE](LICENSE).
