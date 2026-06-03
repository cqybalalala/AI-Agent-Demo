"""
Bank statement CSV parser.
Normalises common Malaysian bank export formats into a standard list of dicts:
  { date, description, amount, currency, reference }
"""

import csv
import io
import re
from datetime import datetime


def _parse_date(raw: str) -> str:
    """Try common date formats, return YYYY-MM-DD or original string."""
    raw = raw.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _clean_amount(raw: str) -> float:
    """Remove currency symbols, commas, spaces; return float."""
    cleaned = re.sub(r"[^\d.\-]", "", raw.replace(",", ""))
    return float(cleaned) if cleaned else 0.0


def _detect_columns(header: list[str]) -> dict:
    """
    Fuzzy-match header names to standard fields.
    Returns mapping: { standard_field: column_index }
    """
    h = [c.lower().strip() for c in header]

    def find(candidates):
        for cand in candidates:
            for i, col in enumerate(h):
                if cand in col:
                    return i
        return None

    return {
        "date":        find(["date", "tarikh", "transaction date", "value date"]),
        "description": find(["description", "particulars", "details", "narration", "remarks"]),
        "amount":      find(["amount", "jumlah", "transaction amount"]),
        "debit":       find(["debit"]),
        "credit":      find(["credit"]),
        "currency":    find(["currency", "ccy", "curr"]),
        "reference":   find(["reference", "ref", "chq", "cheque", "transaction id", "txn", "id"]),
    }


def _sheet_id_from_url(url: str) -> str:
    """Extract the spreadsheet ID from any Google Sheets URL format."""
    import re
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"Cannot extract Sheet ID from URL: {url}")
    return m.group(1)


def fetch_sheet(sheet_url: str, sheet_name: str, default_currency: str = "MYR") -> list[dict]:
    """
    Fetch a named sheet tab from a public Google Sheet and parse it as a bank statement.
    sheet_name: the tab name e.g. "May2026", "Jan2026"
    """
    import httpx
    sheet_id = _sheet_id_from_url(sheet_url)
    csv_url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={sheet_name}"
    )
    r = httpx.get(csv_url, timeout=15, follow_redirects=True)
    if r.status_code != 200:
        raise ValueError(f"Failed to fetch sheet '{sheet_name}': HTTP {r.status_code}")
    if "errorMessage" in r.text or len(r.text.strip()) < 10:
        raise ValueError(f"Sheet '{sheet_name}' not found or empty. Check the tab name.")
    return parse_csv(r.content, default_currency)


def fetch_bank_charges(sheet_url: str, sheet_name: str = "BankCharge") -> dict:
    """
    Fetch the bank-charge rate table from a tab in the same Google Sheet.

    Tab schema (one row per currency, plus a `*` fallback row):
        Currency | Percentage | FlatRate(MYR) | Note
        USD      | 0.05       | 25            | SWIFT + intermediary

    `Percentage` is in PERCENT where 0.05 means 0.05% (not 5%). The fee is later
    computed as `gross_myr * Percentage/100 + FlatRate`. The `Note` column is ignored.

    Returns {currency_upper: {"percent": float, "flat": float}} including a "*" key.
    Raises ValueError if the tab is missing / unreadable.
    """
    import httpx
    sheet_id = _sheet_id_from_url(sheet_url)
    csv_url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={sheet_name}"
    )
    r = httpx.get(csv_url, timeout=15, follow_redirects=True)
    if r.status_code != 200:
        raise ValueError(f"Failed to fetch sheet '{sheet_name}': HTTP {r.status_code}")
    if "errorMessage" in r.text or len(r.text.strip()) < 10:
        raise ValueError(f"Sheet '{sheet_name}' not found or empty. Check the tab name.")

    text = r.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError(f"Sheet '{sheet_name}' is empty.")

    header = [c.lower().strip() for c in rows[0]]

    def col(*cands):
        for cand in cands:
            for i, h in enumerate(header):
                if cand in h:
                    return i
        return None

    ci_cur, ci_pct, ci_flat = col("currency", "ccy"), col("percent", "%"), col("flat", "fixed")
    if ci_cur is None:
        raise ValueError(f"Sheet '{sheet_name}' has no Currency column. Found: {rows[0]}")

    table = {}
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        cur = row[ci_cur].strip().upper() if ci_cur < len(row) else ""
        if not cur:
            continue
        pct  = _clean_amount(row[ci_pct])  if ci_pct  is not None and ci_pct  < len(row) else 0.0
        flat = _clean_amount(row[ci_flat]) if ci_flat is not None and ci_flat < len(row) else 0.0
        table[cur] = {"percent": pct, "flat": flat}

    if not table:
        raise ValueError(f"Sheet '{sheet_name}' has no rows.")
    return table


def parse_csv(file_bytes: bytes, default_currency: str = "MYR") -> list[dict]:
    """
    Parse bank statement CSV bytes into a normalised list of transactions.
    Skips rows where amount is zero or unparseable.
    """
    text = file_bytes.decode("utf-8-sig", errors="replace")  # handle BOM
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        return []

    # Find header row (first row with >2 non-empty cells)
    header_idx = 0
    for i, row in enumerate(rows):
        if sum(1 for c in row if c.strip()) > 2:
            header_idx = i
            break

    header = rows[header_idx]
    col = _detect_columns(header)

    transactions = []
    for row in rows[header_idx + 1:]:
        if not any(c.strip() for c in row):
            continue  # skip blank lines

        def get(field):
            idx = col.get(field)
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        try:
            if col.get("amount") is not None:
                amount = _clean_amount(get("amount"))
            else:
                # Separate debit/credit columns — take whichever is non-zero
                debit  = _clean_amount(get("debit"))  if col.get("debit")  is not None else 0.0
                credit = _clean_amount(get("credit")) if col.get("credit") is not None else 0.0
                amount = debit or credit
        except (ValueError, TypeError):
            continue

        if amount == 0:
            continue

        transactions.append({
            "date":        _parse_date(get("date")) if get("date") else "",
            "description": get("description"),
            "amount":      abs(amount),
            "currency":    get("currency") or default_currency,
            "reference":   get("reference"),
        })

    return transactions
