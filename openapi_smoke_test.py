"""
Moomoo OpenAPI smoke test
-------------------------
Run this AFTER you have:
  1. Enabled OpenAPI on your Moomoo SG account (Me -> Settings -> API Access)
  2. Installed OpenD and logged in (the gateway should show "Connected")
  3. Set your trading password as an env var:  export FUTU_TRD_PWD='your_trading_pin'
  4. Installed the SDK:  pip install futu-api

If all four checks below print OK, OpenAPI is live and we can move to ETL.
"""

import os
import sys
from datetime import datetime

try:
    from futu import (
        OpenQuoteContext,
        OpenSecTradeContext,
        TrdMarket,
        SecurityFirm,
        TrdEnv,
        RET_OK,
    )
except ImportError:
    print("ERROR: futu-api is not installed.  Run:  pip install futu-api")
    sys.exit(1)

# Show which SecurityFirm enum values this SDK build exposes, so we never
# have to guess the right name again.
_firms = [a for a in dir(SecurityFirm) if not a.startswith("_") and a.isupper()]
print(f"Available SecurityFirm values in this SDK: {_firms}")

HOST = "127.0.0.1"
PORT = 11111  # default OpenD port; change if you configured a different one

# ---------------------------------------------------------------------------
# 1. Quote context — market data
# ---------------------------------------------------------------------------
print("\n[1/4] Connecting to OpenD quote channel...")
quote_ctx = OpenQuoteContext(host=HOST, port=PORT)
ret, data = quote_ctx.get_market_snapshot(['US.RKLB'])  # any symbol you hold
if ret == RET_OK:
    row = data.iloc[0]
    print(f"  OK  RKLB last_price = {row['last_price']}  "
          f"({row['update_time']})")
else:
    print(f"  FAIL  {data}")

# ---------------------------------------------------------------------------
# 2. Trade context — Singapore account
#    SecurityFirm.FUTUSECURITIES_SG is the SG entity (Moomoo Financial Singapore)
# ---------------------------------------------------------------------------
print("\n[2/4] Connecting to OpenD trade channel (SG account)...")
trd_ctx = OpenSecTradeContext(
    filter_trdmarket=TrdMarket.SG,            # primary market of the account
    host=HOST, port=PORT,
    security_firm=SecurityFirm.FUTUSG,        # Moomoo Financial Singapore entity
)

# Unlock trading (required before any account/order query that touches real funds)
pwd = os.environ.get("FUTU_TRD_PWD")
if not pwd:
    print("  WARN: FUTU_TRD_PWD env var not set. Some queries may fail.")
else:
    ret, data = trd_ctx.unlock_trade(pwd)
    print(f"  unlock_trade -> {'OK' if ret == RET_OK else data}")

# ---------------------------------------------------------------------------
# 3. Account info
# ---------------------------------------------------------------------------
print("\n[3/4] Querying account info...")
ret, data = trd_ctx.accinfo_query(trd_env=TrdEnv.REAL)
if ret == RET_OK:
    # Useful columns: total_assets, cash, market_val, currency, power
    print(data.to_string(index=False))
else:
    print(f"  FAIL  {data}")

# ---------------------------------------------------------------------------
# 4. Positions
# ---------------------------------------------------------------------------
print("\n[4/4] Querying current positions...")
ret, data = trd_ctx.position_list_query(trd_env=TrdEnv.REAL)
if ret == RET_OK:
    cols = ['code', 'stock_name', 'qty', 'cost_price', 'nominal_price',
            'market_val', 'pl_ratio', 'pl_val', 'currency']
    cols = [c for c in cols if c in data.columns]
    print(data[cols].to_string(index=False))
else:
    print(f"  FAIL  {data}")

quote_ctx.close()
trd_ctx.close()
print(f"\nDone at {datetime.now().isoformat(timespec='seconds')}")
