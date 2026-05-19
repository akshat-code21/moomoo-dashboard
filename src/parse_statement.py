"""
Moomoo SG monthly statement parser.

Public entry point:  parse_statement(pdf_path: Path) -> ParsedStatement

The parser extracts three structured blocks from each PDF:
  1. nav_start / nav_end   per-currency cash, plus the FX rate to SGD
  2. cash_events            every cash ledger event (Cash In Out, Fund Sub/Red,
                            Currency Exchange, Corporate Action, etc.)
  3. positions              ending-positions snapshot per security

After extraction we *reconcile*: for each currency, sum(cash_events) by type
should equal the page-1 "Changes in Cash" summary. We tolerate 0.05 of rounding.
If reconciliation fails we raise ReconciliationError — better to halt loudly
than silently land bad data.

Tested against: Moomoo SG margin-account monthly statement layout, Apr 2026.
Bump PARSER_VERSION whenever this parser logic changes.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
import hashlib
import re
from typing import Optional

import pdfplumber

PARSER_VERSION = "2026.05.15.1"

# Regexes
_DATE_TIME_RE = re.compile(r"^(\d{4}/\d{2}/\d{2}) (\d{2}:\d{2}:\d{2})\s+(.*)$")
_AMOUNT_RE = re.compile(r"([-+]?[\d,]+\.\d{2})")
_FX_IN_COMMENT_RE = re.compile(
    r"\(([A-Z]{3})\s*->\s*([A-Z]{3})\s+([\d.]+)\)"
)
_CCY_HEADER_RE = re.compile(r"^([A-Z]{3})\s+Date/Time\s+Type\s+Amount\s+Comment\s*$")
_PERIOD_RE = re.compile(r"^([A-Z][a-z]+)\s+(\d{4})\s*$")  # "Apr 2026"
_PREP_DATE_RE = re.compile(r"Preparation Date:\s*(\d{4}/\d{2}/\d{2})")

# Cash event types we recognise. LONGEST prefix must come first because we
# use a startswith() match (otherwise "Fund Subscription Fee" gets collapsed
# into "Fund Subscription").
_EVENT_TYPES = [
    "Fund Subscription Fee",
    "Fund Redemption Fee",
    "Fund Corporate Action",
    "Fund Subscription",
    "Fund Redemption",
    "Cash In Out",
    "Currency Exchange",
    "Corporate Action",
    "Asset Adjustment",
]

# Maps page-1 "Changes in Cash" summary labels to cash-ledger event types.
# Anything not in this map is skipped during reconciliation (e.g. Buy/Sell
# Amount/Fee come from the trades section, not the cash ledger).
_SUMMARY_TO_EVENT_TYPE = {
    "Cash In Out": "Cash In Out",
    "Currency Exchange": "Currency Exchange",
    "Fund Corporate Action": "Fund Corporate Action",
    "Corporate Action": "Corporate Action",
    "Subscription Amount": "Fund Subscription",
    "Redemption Amount": "Fund Redemption",
}

# Lines on the left "summary" column of the cash pages that we want to ignore.
# They are not events; they're metadata that pdfplumber interleaves into the
# event stream because of the two-column page layout.
_SUMMARY_PREFIXES = (
    "Starting Cash", "Ending Cash", "Ending Settled Cash", "Ending Unsettled Cash",
    "- Buy Amount", "- Buy Fee", "- Sell Amount", "- Sell Fee",
    "Subscription Amount", "Redemption Amount", "Total",
)

# Lines that terminate the current ccy ledger section. We match these AS A
# WHOLE-LINE regex (not startswith) because they contain numbers.
_SECTION_TERMINATOR_RE = re.compile(
    r"^([A-Z]{3})\s+Total\s+[-+\d,.]+", re.IGNORECASE
)


class ReconciliationError(Exception):
    pass


@dataclass
class CashEvent:
    event_time: datetime
    ccy: str
    event_type: str
    amount: float
    comment: str = ""
    fx_rate: Optional[float] = None

    @property
    def row_hash(self) -> str:
        key = f"{self.event_time.isoformat()}|{self.ccy}|{self.event_type}|{self.amount:.4f}|{self.comment}"
        return hashlib.sha256(key.encode()).hexdigest()


@dataclass
class Position:
    snapshot_date: date
    symbol: str
    exchange: str
    ccy: str
    settled_qty: float
    unsettled_qty: float
    closing_price: float
    market_value: float
    fx_rate_to_sgd: float
    market_value_sgd: float


@dataclass
class ParsedStatement:
    pdf_path: Path
    sha256: str
    period_start: date
    period_end: date
    nav_start: dict[str, float] = field(default_factory=dict)   # ccy -> total assets per ccy (cash + positions)
    nav_end: dict[str, float] = field(default_factory=dict)
    fx_start: dict[str, float] = field(default_factory=dict)    # ccy -> rate-to-SGD
    fx_end: dict[str, float] = field(default_factory=dict)
    summary: dict[str, dict[str, float]] = field(default_factory=dict)  # ccy -> {type: amount}
    cash_events: list[CashEvent] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    # Actual ending cash per currency, read from the per-ccy cash ledger
    # sections ("Ending Cash X" lines). This is the TRUE cash balance — NOT
    # the per-ccy NAV in nav_end which includes positions.
    ending_cash: dict[str, float] = field(default_factory=dict)
    starting_cash: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_amount(s: str) -> float:
    return float(s.replace(",", "").replace("+", ""))


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    from datetime import timedelta
    return date(year, month + 1, 1) - timedelta(days=1)


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _parse_period(text: str) -> tuple[date, date]:
    """Read 'Apr 2026' from the header and turn into (period_start, period_end)."""
    for line in text.splitlines()[:5]:
        m = _PERIOD_RE.match(line.strip())
        if m:
            month_name, year = m.group(1), int(m.group(2))
            month = datetime.strptime(month_name, "%b").month
            ps = date(year, month, 1)
            return ps, _month_end(year, month)
    raise ValueError("Could not find period header (e.g. 'Apr 2026') in PDF")


def _parse_page1_nav_and_fx(text: str, out: ParsedStatement) -> None:
    """
    Pull starting and ending NAV-per-currency and FX rates from page 1.
    The page layout looks like:
        Starting Net Asset Value YYYYMMDD
        SGD USD HKD CNH JPY
        Equal to(SGD)
        3,976.96 4,443.41 2,429.04 0.00 0.00
        10,089.74
        exchange rate : 1.000000 exchange rate : 1.285955 ...
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if line.startswith("Starting Net Asset Value"):
            target = out.nav_start
            fx_target = out.fx_start
        elif line.startswith("Ending Net Asset Value"):
            target = out.nav_end
            fx_target = out.fx_end
        else:
            continue
        # next "ccy header line" should be e.g. "SGD USD HKD CNH JPY"
        ccy_line = lines[i + 1].split()
        # values line is i+3 (i+2 is "Equal to(SGD)")
        values_line = lines[i + 3].split()
        for ccy, val in zip(ccy_line, values_line):
            try:
                target[ccy] = _parse_amount(val)
            except ValueError:
                pass
        # exchange rate line
        ex_line = lines[i + 5]
        rates = re.findall(r"exchange rate\s*:\s*([\d.\-]+)", ex_line)
        for ccy, rate_str in zip(ccy_line, rates):
            try:
                fx_target[ccy] = float(rate_str)
            except ValueError:
                fx_target[ccy] = None  # "-" for unused currencies


def _parse_page1_summary(text: str, out: ParsedStatement) -> None:
    """
    Parse the 'Changes in Cash' matrix on page 1 — currency columns and
    one row per event-type. Populates out.summary[ccy][event_type].
    """
    lines = text.splitlines()
    # find the header line
    header_idx = None
    for i, line in enumerate(lines):
        if re.match(r"\+?\s*Changes in Cash\s+SGD\s+USD\s+HKD\s+CNH\s+JPY", line):
            header_idx = i
            break
    if header_idx is None:
        return
    ccys = ["SGD", "USD", "HKD", "CNH", "JPY"]
    for ccy in ccys:
        out.summary.setdefault(ccy, {})
    # parse rows until we hit a non-data line
    num_re = re.compile(r"[-+]?[\d,]+\.\d{2}")
    for line in lines[header_idx + 1:]:
        if not line.strip() or "Changes in" in line or "Preparation Date" in line:
            break
        # row format: "<event type words> v1 v2 v3 v4 v5"
        matches = list(num_re.finditer(line))
        if len(matches) < 5:
            continue
        # Label is whatever comes before the FIFTH-FROM-LAST number's start.
        fifth_from_last = matches[-5]
        label = line[:fifth_from_last.start()].strip()
        if not label:
            continue
        vals = [_parse_amount(m.group()) for m in matches[-5:]]
        for ccy, v in zip(ccys, vals):
            # Multiple "Corporate Action" rows can exist; sum them.
            out.summary[ccy][label] = out.summary[ccy].get(label, 0.0) + v


# Allow trailing text after the amount because pdfplumber sometimes pastes
# right-column event text onto these summary lines (e.g.
# "Ending Cash 226.86 Fund Redemption#...").
_STARTING_CASH_RE = re.compile(r"^Starting Cash\s+([-+\d,.]+)(?:\s+.*)?$")
_ENDING_CASH_RE = re.compile(r"^Ending Cash\s+([-+\d,.]+)(?:\s+.*)?$")


def _parse_cash_balances(text: str, out: ParsedStatement) -> None:
    """
    Walk the text and capture the 'Starting Cash X' and 'Ending Cash X' lines
    inside each per-currency cash ledger section. These are the TRUE cash
    balances at period start/end; we use them for reconciliation rather than
    the per-currency NAV (which includes positions).

    Carefully skips 'Ending Settled Cash' / 'Ending Unsettled Cash' lines —
    we only want the unqualified 'Ending Cash X'.
    """
    current_ccy = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _CCY_HEADER_RE.match(line)
        if m:
            current_ccy = m.group(1)
            continue
        if _SECTION_TERMINATOR_RE.match(line):
            current_ccy = None
            continue
        if current_ccy is None:
            continue
        sm = _STARTING_CASH_RE.match(line)
        if sm and current_ccy not in out.starting_cash:
            out.starting_cash[current_ccy] = _parse_amount(sm.group(1))
            continue
        em = _ENDING_CASH_RE.match(line)
        if em and current_ccy not in out.ending_cash:
            out.ending_cash[current_ccy] = _parse_amount(em.group(1))


def _parse_cash_events(text: str, out: ParsedStatement) -> None:
    """
    Walk every line. When we see a currency header ('SGD Date/Time Type Amount Comment'),
    switch context. Then any line beginning with YYYY/MM/DD HH:MM:SS is a new event.
    Subsequent non-date lines are either summary chrome (ignore) or comment continuation.
    """
    current_ccy: Optional[str] = None
    last_event: Optional[CashEvent] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            last_event = None
            continue

        m = _CCY_HEADER_RE.match(line)
        if m:
            current_ccy = m.group(1)
            last_event = None
            continue

        if current_ccy is None:
            continue

        # New event?
        m = _DATE_TIME_RE.match(line)
        if m:
            ymd, hms, rest = m.groups()
            # rest looks like "Fund Subscription -500.00 [optional comment]".
            # The event amount is the FIRST amount on the line — anything
            # else (e.g. "0.29677354 USD PER SHARE") is comment text.
            amts = list(_AMOUNT_RE.finditer(rest))
            if not amts:
                continue
            amt_match = amts[0]
            amount = _parse_amount(amt_match.group(1))
            event_type = rest[:amt_match.start()].strip()
            comment = rest[amt_match.end():].strip()
            # Strip event_type down to known label if it matches one
            for et in _EVENT_TYPES:
                if event_type.startswith(et):
                    event_type = et
                    break
            evt = CashEvent(
                event_time=datetime.strptime(f"{ymd} {hms}", "%Y/%m/%d %H:%M:%S"),
                ccy=current_ccy,
                event_type=event_type,
                amount=amount,
                comment=comment,
            )
            out.cash_events.append(evt)
            last_event = evt
            continue

        # End-of-section line ("SGD Total -993.57", "USD Total -1,005.47", etc.)
        if _SECTION_TERMINATOR_RE.match(line):
            current_ccy = None
            last_event = None
            continue

        # Not a date line. Is it summary chrome we should ignore?
        if any(line.startswith(p) for p in _SUMMARY_PREFIXES):
            last_event = None
            continue
        if "Preparation Date" in line or "Client Name" in line:
            last_event = None
            continue

        # Otherwise: comment continuation for the previous event.
        if last_event is not None:
            last_event.comment = (last_event.comment + " " + line).strip()

    # Post-process: extract FX rate from Currency Exchange comments.
    for evt in out.cash_events:
        if evt.event_type == "Currency Exchange":
            m = _FX_IN_COMMENT_RE.search(evt.comment)
            if m:
                evt.fx_rate = float(m.group(3))


# Used with re.search (NOT match): an optional inline-prefix (ticker or name
# continuation) may precede the exchange+ccy+numbers block.
_POSITION_LINE_RE = re.compile(
    r"(?P<prefix>(?:\S+\s+)*)"                   # optional inline prefix (multi-word)
    r"(?P<exchange>US|SG|SGX|SEHK|JP|HK|FD|--)\s+"
    r"(?P<ccy>USD|SGD|HKD|JPY|CNH)\s+"
    r"(?P<settled>[\d,]+(?:\.\d+)?|\-\-)\s+"
    r"(?P<unsettled>[\d,]+(?:\.\d+)?|\-\-)\s+"
    r"(?P<qty>[\d,]+(?:\.\d+)?)\s+"
    r"(?P<multiplier>\d+|\-\-)\s+"
    r"(?P<price>[\d,]+\.\d+)\s+"
    r"(?P<mv>[\d,]+\.\d+)\s+"
    r"(?P<fx>[\d.]+)\s+"
    r"(?P<mv_sgd>[\d,]+\.\d+)\s*$"
)

# Words that look like tickers but aren't — they're security-name continuations
# bleeding into the line right after a data row.
_NOT_TICKERS = {
    "ETF", "FUND", "TRUST", "EQUITY", "CALL", "BANK", "STOCK", "ACTIVE",
    "MEMORY", "SHARES", "REIT", "INC", "LTD", "PLC", "CORP", "GROUP",
    "HOLDINGS", "INTERNATIONAL", "MDIS", "INDEX", "DAILY", "LONG", "SHORT",
    "FUTURE", "MOBILITY", "DIVIDEND", "INCOME", "GROWTH", "SEMICONDUCTOR",
    "TECHNOLOGY", "RESOURCES", "PHARMA",
}


def _is_ticker_like(s: str) -> bool:
    """Return True if s plausibly is a Moomoo ticker (e.g. RKLB, 02802, 221A, SG9999002406)."""
    s = s.strip()
    if not (1 <= len(s) <= 14):
        return False
    if " " in s or s.upper() != s:
        return False
    if not s.replace("-", "").replace(".", "").isalnum():
        return False
    if s.upper() in _NOT_TICKERS:
        return False
    return True


def _parse_positions(pdf, out: ParsedStatement) -> None:
    """
    Parse the 'Ending Positions' tables on the last few content pages.

    pdfplumber's table extraction collapses each position row into a single
    multi-line cell, so we operate on the raw text. A position row has this
    shape on its own line:
        <Exchange> <Ccy> <settled> <unsettled> <qty> <multiplier> <price>
        <market_value> <fx_rate> <market_value_sgd>
    The ticker is usually on the line immediately after; the security name
    is on the line(s) immediately before.
    """
    end_date = out.period_end
    in_positions = False

    for page in pdf.pages:
        text = page.extract_text() or ""
        if "Ending Positions" in text:
            in_positions = True
        if not in_positions:
            continue
        if "Important Notice" in text:
            break

        lines = [ln.rstrip() for ln in text.splitlines()]
        for idx, line in enumerate(lines):
            line_stripped = line.strip()
            m = _POSITION_LINE_RE.match(line_stripped)
            if not m:
                continue
            d = m.groupdict()

            # Ticker resolution: prefer an inline prefix on the SAME line if
            # it looks like a ticker (e.g. "42C SGX SGD ..."); otherwise scan
            # the next 4 lines for a ticker-like token.
            symbol = ""
            inline = (d.get("prefix") or "").strip()
            # If the prefix is a single ticker-like token, use it directly.
            # If it's multi-word, the last token might still be the ticker.
            inline_tokens = inline.split() if inline else []
            if inline_tokens and _is_ticker_like(inline_tokens[-1]):
                symbol = inline_tokens[-1]
            if not symbol:
                for look in range(1, 5):
                    if idx + look >= len(lines):
                        break
                    cand = lines[idx + look].strip()
                    if _is_ticker_like(cand):
                        symbol = cand
                        break

            def _to_float(s: str) -> float:
                if s == "--":
                    return 0.0
                return float(s.replace(",", ""))

            out.positions.append(Position(
                snapshot_date=end_date,
                symbol=symbol,
                exchange=d["exchange"],
                ccy=d["ccy"],
                settled_qty=_to_float(d["settled"]),
                unsettled_qty=_to_float(d["unsettled"]),
                closing_price=_to_float(d["price"]),
                market_value=_to_float(d["mv"]),
                fx_rate_to_sgd=_to_float(d["fx"]),
                market_value_sgd=_to_float(d["mv_sgd"]),
            ))


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _reconcile(out: ParsedStatement, tolerance: float = 0.05) -> list[str]:
    """
    For each currency, group cash_events by event_type, sum the amount, and
    compare against the matching page-1 summary line. Labels in the summary
    map to event_types in the ledger via _SUMMARY_TO_EVENT_TYPE.

    Buy/Sell Amount/Fee are excluded because they come from the trades section
    (pages 4-7), which the v1 parser doesn't ingest yet.
    """
    issues = []
    for ccy, summary_rows in out.summary.items():
        # bucket detail events
        detail: dict[str, float] = {}
        for e in out.cash_events:
            if e.ccy != ccy:
                continue
            detail[e.event_type] = detail.get(e.event_type, 0.0) + e.amount
        for label, expected in summary_rows.items():
            event_type = _SUMMARY_TO_EVENT_TYPE.get(label)
            if event_type is None:
                continue  # not a ledger-side row (e.g. Buy Amount, Total)
            actual = detail.get(event_type, 0.0)
            if abs(actual - expected) > tolerance:
                issues.append(
                    f"[{ccy}] {label} -> {event_type}: "
                    f"detail {actual:.2f} vs summary {expected:.2f}"
                )
    return issues


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_statement(pdf_path: Path) -> ParsedStatement:
    pdf_path = Path(pdf_path)
    sha = _file_sha256(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        # Older Moomoo SG statements render bold text by overlaying two copies
        # of every character, which pdfplumber sees as "AAccccoouunntt" etc.
        # dedupe_chars(tolerance=1) collapses the overlap. It is a no-op on
        # newer statements that don't have the overlap.
        deduped_pages = [p.dedupe_chars(tolerance=1) for p in pdf.pages]
        full_text = "\n".join((p.extract_text() or "") for p in deduped_pages)
        period_start, period_end = _parse_period(full_text)
        out = ParsedStatement(
            pdf_path=pdf_path, sha256=sha,
            period_start=period_start, period_end=period_end,
        )
        _parse_page1_nav_and_fx(full_text, out)
        _parse_page1_summary(full_text, out)
        _parse_cash_balances(full_text, out)   # real ending cash per ccy
        _parse_cash_events(full_text, out)
        # Position parser walks pages too; pass the deduped list.
        class _PdfShim:
            def __init__(self, pages): self.pages = pages
        _parse_positions(_PdfShim(deduped_pages), out)

    issues = _reconcile(out)
    if issues:
        msg = "Reconciliation FAILED for " + pdf_path.name + "\n  " + "\n  ".join(issues)
        raise ReconciliationError(msg)
    return out


if __name__ == "__main__":
    import sys
    p = Path(sys.argv[1])
    res = parse_statement(p)
    print(f"Parsed {p.name}")
    print(f"  Period: {res.period_start} -> {res.period_end}")
    print(f"  NAV start (cash): {res.nav_start}")
    print(f"  NAV end   (cash): {res.nav_end}")
    print(f"  FX  end: {res.fx_end}")
    print(f"  Cash events: {len(res.cash_events)}")
    print(f"  Positions:   {len(res.positions)}")
    # Highlight capital flows
    flows = [e for e in res.cash_events if e.event_type == 'Cash In Out']
    print(f"\n  Capital In/Out events: {len(flows)}")
    for e in flows:
        print(f"    {e.event_time}  {e.ccy} {e.amount:+.2f}  {e.comment}")
