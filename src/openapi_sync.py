"""
Pull current cash + positions from OpenD via the futu-api SDK and write into
live_*_snapshots. Pure read; never places trades.

Pre-reqs (same as openapi_smoke_test.py):
    - OpenD running and logged in
    - export FUTU_TRD_PWD='your_trading_pin'

Usage:
    python3 -m src.openapi_sync
"""

from __future__ import annotations
import os
import sys
from datetime import datetime

import pandas as pd
from futu import (
    OpenQuoteContext, OpenSecTradeContext,
    TrdMarket, SecurityFirm, TrdEnv, RET_OK,
)

from src.db import Session, create_all, LiveCashSnapshot, LivePositionSnapshot

HOST = "127.0.0.1"
PORT = 11111


def sync() -> dict:
    create_all()
    now = datetime.utcnow()

    trd_ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.SG,
        host=HOST, port=PORT,
        security_firm=SecurityFirm.FUTUSG,
    )

    # Newer OpenD versions require unlocking via the GUI's "Unlock" button;
    # the API path is informational. We'll still try in case the user is on
    # an older build.
    pwd = os.environ.get("FUTU_TRD_PWD")
    if pwd:
        ret, msg = trd_ctx.unlock_trade(pwd)
        if ret != RET_OK:
            print(f"  unlock_trade (info, not fatal): {str(msg)[:120]}")

    summary = {"cash_rows": 0, "position_rows": 0}

    # ---- Cash / account totals ----
    # accinfo_query for SG accounts returns ONE row with many columns,
    # including per-currency cash columns (hk_cash, us_cash, jp_cash,
    # sg_cash, cn_cash, au_cash, ca_cash, my_cash). The generic "cash"
    # column is an internal aggregate in HKD and not what we want — we
    # extract the per-currency columns explicitly instead.
    ret, data = trd_ctx.accinfo_query(trd_env=TrdEnv.REAL)
    # Map from accinfo column -> our currency code
    _CCY_COL_MAP = {
        "sg_cash": "SGD",
        "us_cash": "USD",
        "hk_cash": "HKD",
        "jp_cash": "JPY",
        "cn_cash": "CNH",
        "au_cash": "AUD",
        "ca_cash": "CAD",
        "my_cash": "MYR",
    }

    def _safe_float(v) -> float:
        """Coerce SDK values that may be 'N/A', None, '' or a real number."""
        if v is None:
            return 0.0
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    with Session() as session:
        if ret == RET_OK and isinstance(data, pd.DataFrame) and not data.empty:
            print(f"  accinfo columns: {list(data.columns)}")
            r = data.iloc[0]
            row_currency = str(r.get("currency", "") or "").upper()
            row_total_assets = _safe_float(r.get("total_assets"))

            for col, ccy in _CCY_COL_MAP.items():
                if col not in data.columns:
                    continue
                cash = _safe_float(r.get(col))
                if cash == 0:
                    continue
                total_sgd = row_total_assets if (ccy == "SGD" and row_currency == "SGD") else None
                session.add(LiveCashSnapshot(
                    snapshot_time=now, ccy=ccy,
                    cash=cash, total_assets_sgd=total_sgd,
                ))
                summary["cash_rows"] += 1
        else:
            print(f"  accinfo_query FAIL: {data}")

        # ---- Positions ----
        ret, data = trd_ctx.position_list_query(trd_env=TrdEnv.REAL)
        if ret == RET_OK and isinstance(data, pd.DataFrame) and not data.empty:
            for _, r in data.iterrows():
                code = str(r.get("code", ""))
                symbol = code.split(".", 1)[1] if "." in code else code
                session.add(LivePositionSnapshot(
                    snapshot_time=now, symbol=symbol,
                    qty=_safe_float(r.get("qty")),
                    cost_price=_safe_float(r.get("cost_price")),
                    nominal_price=_safe_float(r.get("nominal_price")),
                    market_value=_safe_float(r.get("market_val")),
                    pl_val=_safe_float(r.get("pl_val")),
                    ccy=str(r.get("currency", "")).upper().replace("HK_", ""),
                ))
                summary["position_rows"] += 1
        else:
            print(f"  position_list_query FAIL: {data}")

        session.commit()

    trd_ctx.close()
    summary["snapshot_time"] = now.isoformat()
    return summary


if __name__ == "__main__":
    print(sync())
