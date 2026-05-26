"""
Tool definitions + handlers.
TOOL_DEFINITIONS  → sent to the LLM (JSON schema)
execute_tool()    → called when LLM returns a tool_call
"""
import difflib
import json
import os
import subprocess
import sys
from erpnext_client import ERPAdapter

# ── Matching helpers (three-way reconciliation) ──────────────────────────────

def name_similarity(a: str, b: str) -> float:
    """Fuzzy name match ratio in [0,1]. Case-insensitive."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, str(a).lower().strip(), str(b).lower().strip()).ratio()

def amount_score(target: float, candidate: float, tol: float) -> tuple:
    """Returns (within_tolerance, score in [0,1]) where score=1 at exact match, 0 at the tol edge."""
    if not target:
        return (False, 0.0)
    diff = abs(target - candidate) / target
    return (diff <= tol, max(0.0, 1.0 - diff / tol) if tol else 0.0)

def ref_contains(reference: str, *fields) -> bool:
    """True if the (non-empty) reference appears inside any of the given fields."""
    ref = (reference or "").strip().lower()
    if not ref:
        return False
    return any(ref in str(f or "").lower() for f in fields)

# ── Python sandbox ────────────────────────────────────────────────────────────

def run_calc(code: str) -> dict:
    """Run a short Python snippet for calculations. No ERP access."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return {"success": False, "error": result.stderr[-500:]}
    return {"success": True, "output": result.stdout.strip()}

def run_python(code: str) -> dict:
    """Run user code with ERP helpers pre-injected."""
    import config
    prelude = f"""
import httpx, json
from collections import defaultdict

ERP_URL = {repr(config.ERPNEXT_URL)}
HEADERS = {{"Authorization": "token {config.ERPNEXT_API_KEY}:{config.ERPNEXT_SECRET}"}}

def erp_get(path, params=None):
    encoded = path.replace(" ", "%20")
    r = httpx.get(f"{{ERP_URL}}{{encoded}}", headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()
"""
    full_code = prelude + "\n" + code
    result = subprocess.run(
        [sys.executable, "-c", full_code],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        return {"success": False, "error": result.stderr[-1000:]}
    return {"success": True, "output": result.stdout[-3000:]}

# ── Docs search ───────────────────────────────────────────────────────────────

DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")

def search_docs(query: str) -> str:
    """Search docs/ for sections matching query keywords. Returns matching sections."""
    keywords = [w.lower() for w in query.split() if len(w) > 2]
    matches = []
    for fname in os.listdir(DOCS_DIR):
        if not fname.endswith(".md"):
            continue
        with open(os.path.join(DOCS_DIR, fname), encoding="utf-8") as f:
            content = f.read()
        # Split into sections by ## headers
        sections = ["## " + s for s in content.split("## ") if s.strip()]
        for section in sections:
            section_lower = section.lower()
            if any(kw in section_lower for kw in keywords):
                matches.append(section.strip())
    if not matches:
        return f"No documentation found for query: '{query}'. Available files: {os.listdir(DOCS_DIR)}"
    return "\n\n---\n\n".join(matches)

# ── Tool schemas (what the LLM sees) ─────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": (
                "Execute Python code to perform accurate arithmetic, percentage, rounding, "
                "date, or aggregation calculations. "
                "Use this ANY TIME you need to compute a number — never do math in your head. "
                "Always print() the final result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Python code to execute. Must call print() to output the result. "
                            "Examples: 'print(1234 * 0.06)' or 'print(round(500/3, 2))' or "
                            "'from datetime import date; print((date(2026,6,1)-date.today()).days)'"
                        ),
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_docs",
            "description": (
                "Search local documentation for ERPNext report names, filter keys, and usage examples. "
                "Call this FIRST when you are unsure about a report name or its required filters. "
                "Examples: 'Gross Profit filters', 'Purchase Analytics', 'stock report'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords to search for e.g. 'Gross Profit filters'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_fields",
            "description": (
                "Get the field structure of an ERPNext DocType. "
                "Call this FIRST when you are unsure of field names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doctype": {"type": "string", "description": "e.g. 'Sales Order'"},
                },
                "required": ["doctype"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_list",
            "description": (
                "List ERPNext documents, or aggregate with group_by+sum_field. "
                "For aggregation (totals per supplier, revenue per customer, etc.), "
                "use group_by and sum_field — all pages are fetched automatically. "
                "For regular listing, results are capped at limit rows. "
                "DO NOT use this to find what items were bought or sold — use erpnext_items instead. "
                "DO NOT use dot-notation like 'items.item_code' — it will fail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doctype":    {"type": "string"},
                    "filters":    {
                        "type": "array",
                        "description": "Frappe filter tuples e.g. [[\"status\",\"=\",\"Draft\"]]",
                        "items": {},
                    },
                    "fields":     {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Fields to return. For financial documents always include 'currency'.",
                    },
                    "limit":      {"type": "integer", "description": "Max rows for regular listing (default 20, ignored when group_by is set)"},
                    "order_by":   {"type": "string",  "description": "e.g. 'modified desc'"},
                    "group_by":   {"type": "string",  "description": "Field to group by for aggregation e.g. 'supplier'"},
                    "sum_field":  {"type": "string",  "description": "Numeric field to sum per group e.g. 'grand_total'"},
                },
                "required": ["doctype"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_get",
            "description": "Get a single document by its exact name/ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doctype": {"type": "string"},
                    "name":    {"type": "string", "description": "e.g. 'SAL-ORD-2024-00001'"},
                },
                "required": ["doctype", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_linked",
            "description": (
                "Find documents linked to a given document. "
                "Use this to find Sales Invoices for a Sales Order, "
                "Purchase Invoices for a Purchase Order, Delivery Notes for a Sales Order, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_doctype": {"type": "string", "description": "e.g. 'Sales Order'"},
                    "source_name":    {"type": "string", "description": "e.g. 'SAL-ORD-2026-00346'"},
                    "target_doctype": {"type": "string", "description": "e.g. 'Sales Invoice'"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Fields to return from target doctype",
                    },
                },
                "required": ["source_doctype", "source_name", "target_doctype"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_report",
            "description": (
                "Run an ERPNext built-in report and get the results. "
                "Prefer this over erpnext_items for item/financial analysis — one request, server-side aggregation. "
                "Useful reports and their filter keys:\n"
                "- 'Purchase Analytics': from_date, to_date, company, based_on='Item' (REQUIRED)\n"
                "- 'Sales Analytics': DO NOT USE — use erpnext_sales_chart or erpnext_items instead.\n"
                "- 'Gross Profit': from_date, to_date, company, group_by='Invoice'/'Item Code'/'Customer'/'Customer Group'/'Brand' — MUST include group_by\n"
                "- 'Accounts Receivable': report_date, ageing_based_on='Due Date'/'Posting Date', company\n"
                "Always include 'company' in filters. If unsure of other filter keys, omit them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "report_name": {"type": "string", "description": "Exact ERPNext report name e.g. 'Purchase Analytics'"},
                    "filters": {"type": "object", "description": "Report filter dict e.g. {\"from_date\": \"2025-01-01\", \"to_date\": \"2025-12-31\", \"based_on\": \"Item\"}"},
                    "top_n":   {"type": "integer", "description": "Return only the top N rows sorted by largest value (default 20)"},
                },
                "required": ["report_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_items",
            "description": (
                "Aggregate line items across all matching parent documents. "
                "Use this for questions like 'what items did we buy/sell most', "
                "'top products by revenue', 'most ordered items this year'. "
                "Fetches full parent documents to extract items — works even when "
                "child doctypes have permission restrictions. "
                "Parent doctypes: 'Purchase Invoice', 'Sales Invoice', "
                "'Purchase Order', 'Sales Order', 'Delivery Note'. "
                "group_by defaults to 'item_code', sum_field defaults to 'amount'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "parent_doctype": {"type": "string", "description": "e.g. 'Purchase Invoice'"},
                    "filters": {
                        "type": "array",
                        "description": "Frappe filter tuples to narrow parent docs e.g. [[\"docstatus\",\"=\",1]]",
                        "items": {},
                    },
                    "group_by":  {"type": "string", "description": "Item field to group by (default 'item_code')"},
                    "sum_field": {"type": "string", "description": "Item field to sum (default 'amount')"},
                },
                "required": ["parent_doctype"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_search",
            "description": (
                "Search for valid document names by text. "
                "Use this to find exact names before referencing records."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doctype": {"type": "string"},
                    "query":   {"type": "string", "description": "Search text"},
                },
                "required": ["doctype", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_execute_sql",
            "description": (
                "Execute raw SQL directly against the ERPNext Database. "
                "Use this for complex analytical questions requiring JOINS or aggregations across tables. "
                "Only submit SELECT queries. Prepend 'tab' to DocTypes to get table names (e.g., 'tabSales Invoice' and 'tabSales Invoice Item'). "
                "For parent tables, use 'name' to join with child table 'parent' field (e.g., ON parent_table.name = child_table.parent)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_query": {"type": "string", "description": "The raw SELECT query. Avoid updates or drops."},
                },
                "required": ["sql_query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_chart_from_sql",
            "description": (
                "Run a SQL query and render the result as a chart. "
                "Use this when the user asks to visualize, plot, chart, or draw data. "
                "The SQL must return at least two columns: one for labels and one for numeric values. "
                "Do NOT use erpnext_execute_sql separately first — this tool runs the SQL and charts it in one step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_query":   {"type": "string", "description": "SELECT query whose result has a label column and a numeric value column."},
                    "chart_type":  {"type": "string", "enum": ["bar", "horizontal-bar", "line", "pie", "donut"], "description": "Chart type. Use 'line' for trends over time, 'bar'/'horizontal-bar' for comparisons, 'pie'/'donut' for proportions."},
                    "title":       {"type": "string", "description": "Chart title shown above the chart."},
                    "label_field": {"type": "string", "description": "Column name from SQL result to use as category labels (X-axis or pie slices)."},
                    "value_field": {"type": "string", "description": "Column name from SQL result to use as numeric values."},
                    "currency":    {"type": "string", "description": "Optional currency label e.g. 'MYR'. Appended to the value axis."},
                },
                "required": ["sql_query", "chart_type", "title", "label_field", "value_field"],
            },
        },
    },
]

# ── Write tool schemas ──────────────────────────────────────────────────────

WRITE_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "erpnext_create",
            "description": (
                "Create a new ERPNext document. "
                "Creates a new ERPNext document in Draft status. "
                "Ensure all required fields are populated before calling — do NOT call with incomplete data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doctype": {"type": "string", "description": "e.g. 'Sales Order', 'Payment Entry'"},
                    "data": {
                        "type": "object",
                        "description": (
                            "Document fields as key-value pairs. "
                            "For child tables (e.g. items), use a list of dicts. "
                            "Example: {\"customer\": \"ABC Corp\", \"delivery_date\": \"2026-04-15\", "
                            "\"items\": [{\"item_code\": \"ITEM-001\", \"qty\": 10, \"rate\": 100}]}"
                        ),
                    },
                },
                "required": ["doctype", "data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_update",
            "description": (
                "Update an existing ERPNext document. "
                "Updates an existing Draft document (docstatus=0). "
                "Ensure all required fields are complete before calling — do NOT call with incomplete data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doctype": {"type": "string", "description": "e.g. 'Sales Order'"},
                    "name": {"type": "string", "description": "Document name e.g. 'SAL-ORD-2026-00400'"},
                    "data": {
                        "type": "object",
                        "description": (
                            "Fields to update as key-value pairs. Only include fields that need to change. "
                            "For child tables (e.g. items), provide the full updated list."
                        ),
                    },
                },
                "required": ["doctype", "name", "data"],
            },
        },
    },
]

# ── Chart tool definitions ────────────────────────────────────────────────────

CHART_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "erpnext_sales_chart",
            "description": (
                "Generate a sales chart. "
                "group_by='customer' → top customers by revenue. "
                "group_by='item' → top selling items. "
                "group_by='month' → monthly revenue trend. "
                "Default chart_type per group: customer→bar, item→bar, month→line. "
                "Use this when the user asks to visualize, chart, or plot sales data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group_by": {
                        "type": "string",
                        "enum": ["customer", "item", "month"],
                        "description": "Dimension to group by (default: customer)",
                    },
                    "chart_type": {
                        "type": "string",
                        "enum": ["bar", "horizontal-bar", "line", "pie", "donut"],
                        "description": "Override default chart type. pie/donut good for showing proportions.",
                    },
                    "limit": {"type": "number", "description": "Top N results (default 10)"},
                    "date_from": {"type": "string", "description": "Start date YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "End date YYYY-MM-DD"},
                    "color": {"type": "string", "description": "Hex color for bars/lines e.g. '#f87171'"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "erpnext_stock_chart",
            "description": (
                "Generate a stock levels chart. "
                "Shows actual_qty per item. Default: horizontal-bar. "
                "Use this when the user asks to visualize or chart stock/inventory levels."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chart_type": {
                        "type": "string",
                        "enum": ["horizontal-bar", "bar", "pie", "donut"],
                        "description": "Chart type (default: horizontal-bar)",
                    },
                    "limit": {"type": "number", "description": "Top N items (default 20)"},
                    "warehouse": {"type": "string", "description": "Filter by warehouse name"},
                    "item_group": {"type": "string", "description": "Filter by item group"},
                    "min_qty": {"type": "number", "description": "Only show items with qty >= this (default 1)"},
                    "color": {"type": "string", "description": "Hex color e.g. '#fb923c'"},
                },
            },
        },
    },
]

# ── Three-way reconciliation tool ────────────────────────────────────────────

THREE_WAY_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "three_way_reconcile",
            "description": (
                "Three-way reconciliation: matches a payment proof against "
                "(1) an outstanding ERPNext invoice and "
                "(2) a bank statement entry from Google Sheets. "
                "Use this immediately after extracting a payment proof image. "
                "Returns all three sides of the match plus FX conversion and a final status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "amount":      {"type": "number",  "description": "Amount from payment proof"},
                    "currency":    {"type": "string",  "description": "Currency from proof e.g. USD, MYR"},
                    "customer":    {"type": "string",  "description": "Sender/customer name from proof"},
                    "reference":   {"type": "string",  "description": "Reference or transaction ID from proof"},
                    "payment_date":{"type": "string",  "description": "Payment date YYYY-MM-DD from proof"},
                    "bank_month":  {"type": "string",  "description": "Bank statement month tab e.g. May2026"},
                    "sheet_url":   {"type": "string",  "description": "Override Google Sheet URL (optional)"},
                    "invoice_name":{"type": "string",  "description": "ERPNext invoice name if already found e.g. ACC-SINV-2026-00545. Pass this to skip the automatic invoice search."},
                    "invoice_type":{"type": "string",  "description": "Sales Invoice or Purchase Invoice (required if invoice_name is provided)"},
                },
                "required": ["amount", "currency", "bank_month"],
            },
        },
    },
]

# ── Create Payment Entry tool ────────────────────────────────────────────────

CREATE_PE_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "create_payment_entry",
            "description": (
                "Create a Payment Entry in ERPNext to mark an invoice as paid. "
                "Use this INSTEAD of erpnext_create for Payment Entry. "
                "Automatically looks up the correct accounts, exchange rate, and links the invoice. "
                "Call this after three_way_reconcile returns RECONCILED or PENDING status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "invoice_name":  {"type": "string", "description": "e.g. ACC-SINV-2026-00728"},
                    "invoice_type":  {"type": "string", "description": "Sales Invoice or Purchase Invoice"},
                    "bank_amount":   {"type": "number", "description": "Amount received in MYR (from bank statement or proof)"},
                    "payment_date":  {"type": "string", "description": "YYYY-MM-DD"},
                    "reference_no":  {"type": "string", "description": "Bank transaction reference number (NOT the invoice name)"},
                },
                "required": ["invoice_name", "invoice_type", "bank_amount", "payment_date", "reference_no"],
            },
        },
    },
]

# ── Forex gain/loss readback tool ────────────────────────────────────────────

FOREX_LOSS_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_payment_forex_loss",
            "description": (
                "Read the realized foreign-exchange gain/loss that ERPNext booked for a "
                "Payment Entry. Call this right after creating a Payment Entry for a "
                "cross-currency payment to report the forex impact. Returns the amount in "
                "company currency (MYR): positive = loss, negative = gain."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payment_entry": {"type": "string", "description": "Payment Entry name e.g. ACC-PAY-2026-00969"},
                },
                "required": ["payment_entry"],
            },
        },
    },
]

# ── Reconciliation report / discrepancy artifact tools ───────────────────────

ARTIFACT_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "generate_reconciliation_report",
            "description": (
                "Generate a downloadable Reconciliation Report for a SUCCESSFUL match. "
                "Call this right after creating the Payment Entry. Pulls the real figures "
                "from the Payment Entry (invoice, FX rate, losses, amounts) and produces a "
                "PDF the user can download."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payment_entry": {"type": "string", "description": "Payment Entry name e.g. ACC-PAY-2026-00972"},
                    "confidence":    {"type": "number", "description": "Match confidence 0-100 from three_way_reconcile (optional)"},
                    "match_method":  {"type": "string", "description": "How it matched: reference / name+amount / amount (optional)"},
                },
                "required": ["payment_entry"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_discrepancy_summary",
            "description": (
                "Generate a downloadable Discrepancy Summary for a payment that could NOT be "
                "reconciled (PARTIAL / PENDING / UNMATCHED / needs review). Explains why the "
                "match failed, grounded in the real candidate data. Produces a downloadable PDF."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payer":         {"type": "string", "description": "Payer/counterparty name from the proof"},
                    "currency":      {"type": "string", "description": "Proof currency"},
                    "amount":        {"type": "number", "description": "Proof amount"},
                    "reference":     {"type": "string", "description": "Proof reference/transaction id"},
                    "payment_date":  {"type": "string", "description": "Proof date YYYY-MM-DD"},
                    "status":        {"type": "string", "description": "PARTIAL / PENDING / UNMATCHED / UNMATCHED_INVOICE"},
                    "reason":        {"type": "string", "description": "Plain explanation of why it did not reconcile"},
                    "closest_invoice": {"type": "string", "description": "Closest candidate invoice name, if any (optional)"},
                    "confidence":    {"type": "number", "description": "Match confidence 0-100 (optional)"},
                    "suggested_action": {"type": "string", "description": "What the user should do next (optional)"},
                },
                "required": ["payer", "status", "reason"],
            },
        },
    },
]

# ── Forex gain/loss analytics tool ───────────────────────────────────────────

FOREX_SUMMARY_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "forex_loss_summary",
            "description": (
                "Summarise realized foreign-exchange gain/loss over a date range, sourced from "
                "ERPNext GL Entries against the Exchange Gain/Loss account (covers BOTH customer "
                "receipts and supplier payments). Use this for questions like 'this week's forex "
                "loss' or 'forex loss this month'. Returns a chart plus totals. "
                "MYR; positive = loss, negative = gain. Do NOT compute forex loss from Payment "
                "Entry difference_amount — that field is not the forex loss."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD (optional; default 30 days before end_date)"},
                    "end_date":   {"type": "string", "description": "YYYY-MM-DD (optional; default today)"},
                    "group_by":   {"type": "string", "description": "day | week | month (default day)"},
                },
            },
        },
    },
]

# ── Bank statement fetch tool ─────────────────────────────────────────────────

BANK_FETCH_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_bank_statement",
            "description": (
                "Fetch a monthly bank statement from Google Sheets. "
                "Each month is a separate sheet tab (e.g. 'Jan2026', 'May2026'). "
                "Call this when the user asks to reconcile a specific month or says "
                "'load the bank statement'. Returns a list of transactions ready for reconciliation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "month": {
                        "type": "string",
                        "description": "Sheet tab name e.g. 'May2026', 'Jan2026', 'Mar2026'",
                    },
                    "sheet_url": {
                        "type": "string",
                        "description": "Override the default Google Sheet URL. Leave blank to use the configured default.",
                    },
                },
                "required": ["month"],
            },
        },
    },
]

# ── Reconciliation tool definition ───────────────────────────────────────────

RECONCILE_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "reconcile_transactions",
            "description": (
                "Bulk-reconcile a list of bank statement transactions against ERPNext invoices. "
                "For each transaction: searches for a matching Sales Invoice or Purchase Invoice "
                "by reference number or amount, fetches the historical forex rate, calculates the "
                "expected local amount, and classifies the match. "
                "Returns a full reconciliation report with status per transaction. "
                "Use this when the user uploads a bank statement or provides multiple payment rows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "transactions": {
                        "type": "array",
                        "description": "List of bank statement rows to reconcile.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "date":        {"type": "string", "description": "Payment date YYYY-MM-DD"},
                                "description": {"type": "string", "description": "Bank transaction description"},
                                "amount":      {"type": "number", "description": "Amount in local currency (MYR)"},
                                "currency":    {"type": "string", "description": "Local currency code e.g. MYR"},
                                "reference":   {"type": "string", "description": "Reference/cheque number"},
                            },
                        },
                    },
                    "invoice_type": {
                        "type": "string",
                        "enum": ["Sales Invoice", "Purchase Invoice", "both"],
                        "description": "Which invoice type to match against. Default: both",
                    },
                },
                "required": ["transactions"],
            },
        },
    },
]

# ── Forex tool definition ────────────────────────────────────────────────────

FOREX_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_forex_rate",
            "description": (
                "Get the exchange rate between two currencies. "
                "Use for cross-border reconciliation: convert invoice amounts to local currency, "
                "or verify a received bank amount matches the invoiced foreign currency amount. "
                "Supports historical rates (pass the payment date) for accurate reconciliation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_currency": {
                        "type": "string",
                        "description": "Source currency ISO code e.g. USD, EUR, GBP",
                    },
                    "to_currency": {
                        "type": "string",
                        "description": "Target currency ISO code e.g. MYR, SGD, JPY",
                    },
                    "date": {
                        "type": "string",
                        "description": "Historical date YYYY-MM-DD. Omit for today's rate.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Optional amount to convert. Returns converted_amount in result.",
                    },
                },
                "required": ["from_currency", "to_currency"],
            },
        },
    },
]

# ── All tools by name (for domain filtering) ────────────────────────────────

ALL_TOOLS = {t["function"]["name"]: t for t in TOOL_DEFINITIONS + WRITE_TOOL_DEFINITIONS + CHART_TOOL_DEFINITIONS + FOREX_TOOL_DEFINITIONS + RECONCILE_TOOL_DEFINITIONS + BANK_FETCH_TOOL_DEFINITIONS + THREE_WAY_TOOL_DEFINITIONS + CREATE_PE_TOOL_DEFINITIONS + FOREX_LOSS_TOOL_DEFINITIONS + ARTIFACT_TOOL_DEFINITIONS + FOREX_SUMMARY_TOOL_DEFINITIONS}


def get_tools_for_domain(read_tool_names: list, write_tool_names: list) -> list:
    """Return tool definition list filtered by domain's allowed tool names."""
    allowed = set(read_tool_names + write_tool_names)
    return [ALL_TOOLS[name] for name in allowed if name in ALL_TOOLS]


# ── Tool execution ────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict, erp: ERPAdapter) -> dict:
    import config
    try:
        if name == "calculate":
            return run_calc(args["code"])

        elif name == "erpnext_docs":
            result = search_docs(args["query"])
            return {"success": True, "content": result}

        elif name == "erpnext_fields":
            fields = erp.get_fields(args["doctype"])
            return {"success": True, "doctype": args["doctype"], "fields": fields}

        elif name == "erpnext_list":
            # Detect child table dot-notation and guide model to correct approach
            all_fields = args.get("fields") or []
            group_by = args.get("group_by") or ""
            sum_field = args.get("sum_field") or ""
            dot_fields = [f for f in all_fields + [group_by, sum_field] if "." in f]
            if dot_fields:
                return {
                    "success": False,
                    "error": (
                        f"Cannot use dot-notation fields {dot_fields} in list query. "
                        f"To query line items, use the child doctype directly. "
                        f"Example: doctype='Purchase Invoice Item', fields=['item_code','qty','amount'], "
                        f"group_by='item_code', sum_field='qty'"
                    )
                }

            limit = min(args.get("limit", 20), config.RESULT_LIMIT)
            result = erp.list(
                doctype=args["doctype"],
                filters=args.get("filters"),
                fields=args.get("fields"),
                limit=limit,
                order_by=args.get("order_by"),
                group_by=args.get("group_by"),
                sum_field=args.get("sum_field"),
            )
            return {"success": True, **result}

        elif name == "erpnext_get":
            doc = erp.get(args["doctype"], args["name"])
            # Strip stale cached stock fields from document line items.
            # These values are snapshots from when the doc was saved — not real-time.
            # Agent must call Bin separately for accurate stock.
            _STALE_STOCK_FIELDS = {
                "actual_qty", "company_total_stock", "projected_qty",
                "ordered_qty", "planned_qty", "production_plan_qty",
                "work_order_qty", "stock_reserved_qty",
            }
            for item in doc.get("items", []):
                for f in _STALE_STOCK_FIELDS:
                    item.pop(f, None)
            return {"success": True, "data": doc}

        elif name == "erpnext_linked":
            rows = erp.linked(
                source_doctype=args["source_doctype"],
                source_name=args["source_name"],
                target_doctype=args["target_doctype"],
                fields=args.get("fields"),
            )
            return {"success": True, "count": len(rows), "data": rows}

        elif name == "erpnext_report":
            result = erp.run_report(
                report_name=args["report_name"],
                filters=args.get("filters"),
                top_n=args.get("top_n", 20),
            )
            return {"success": True, **result}

        elif name == "erpnext_items":
            result = erp.list_items(
                parent_doctype=args["parent_doctype"],
                filters=args.get("filters"),
                group_by=args.get("group_by", "item_code"),
                sum_field=args.get("sum_field", "amount"),
            )
            return {"success": True, **result}

        elif name == "erpnext_search":
            results = erp.search(args["doctype"], args["query"])
            return {"success": True, "results": results}

        elif name == "erpnext_execute_sql":
            result = erp.execute_sql(args["sql_query"])
            return {"success": True, "data": result}

        elif name == "erpnext_chart_from_sql":
            rows = erp.execute_sql(args["sql_query"])
            if not rows:
                return {"success": False, "error": "SQL returned no rows."}
            if rows and "error" in rows[0]:
                return {"success": False, "error": rows[0]["error"]}

            label_field = args["label_field"]
            value_field = args["value_field"]

            # Validate columns exist
            if label_field not in rows[0] or value_field not in rows[0]:
                available = list(rows[0].keys())
                return {
                    "success": False,
                    "error": f"Columns not found. label_field='{label_field}', value_field='{value_field}'. Available: {available}",
                }

            labels = [str(r.get(label_field, "")) for r in rows]
            values = [float(r.get(value_field, 0) or 0) for r in rows]
            currency = args.get("currency", "")
            value_label = value_field.replace("_", " ").title()
            if currency:
                value_label = f"{value_label} ({currency})"

            return {
                "success": True,
                "_type": "chart",
                "chart": {
                    "type":     args["chart_type"],
                    "title":    args["title"],
                    "labels":   labels,
                    "datasets": [{"label": value_label, "values": values, "color": "#60a5fa"}],
                    "currency": currency,
                },
            }

        elif name == "create_payment_entry":
            from forex import get_rate
            inv_name   = args["invoice_name"]
            inv_type   = args["invoice_type"]
            bank_amt   = float(args["bank_amount"])
            pay_date   = args["payment_date"]
            ref_no     = args["reference_no"]
            local_cur  = "MYR"

            # 1. Fetch invoice
            doc = erp.get(inv_type, inv_name)
            if not doc:
                return {"success": False, "error": f"Invoice {inv_name} not found."}

            inv_currency    = doc.get("currency", local_cur)
            inv_outstanding = float(doc.get("outstanding_amount") or doc.get("grand_total") or 0)
            party_field     = "customer" if inv_type == "Sales Invoice" else "supplier"
            party           = doc.get(party_field, "")
            paid_from       = doc.get("debit_to") or doc.get("credit_to", "")

            # 2. Lookup company bank account
            paid_to = ""
            bank_account_doc = ""
            try:
                bank_accts = erp.list(
                    "Bank Account",
                    filters=[["company", "=", config.ERPNEXT_COMPANY], ["is_default", "=", 1]],
                    fields=["name", "account"], limit=1,
                ).get("data", [])
                if not bank_accts:
                    bank_accts = erp.list(
                        "Bank Account",
                        filters=[["company", "=", config.ERPNEXT_COMPANY]],
                        fields=["name", "account"], limit=1,
                    ).get("data", [])
                if bank_accts:
                    paid_to = bank_accts[0].get("account", "")
                    bank_account_doc = bank_accts[0].get("name", "")
            except Exception:
                pass

            # 3. Amounts and exchange rate
            if inv_currency != local_cur:
                paid_amount_pe = inv_outstanding
                fx = get_rate(inv_currency, local_cur, pay_date)
                fx_rate_pe = fx.get("rate", 1.0) if fx.get("success") else 1.0
            else:
                paid_amount_pe = bank_amt
                fx_rate_pe = 1.0

            # 4. Build and create Payment Entry
            pe_data = {
                "payment_type":              "Receive" if party_field == "customer" else "Pay",
                "posting_date":              pay_date,
                "company":                   config.ERPNEXT_COMPANY,
                "mode_of_payment":           "Bank Transfer",
                "party_type":                "Customer" if party_field == "customer" else "Supplier",
                "party":                     party,
                "paid_from":                 paid_from,
                "paid_from_account_currency": inv_currency,
                "bank_account":              bank_account_doc,
                "paid_to":                   paid_to,
                "paid_to_account_currency":  local_cur,
                "paid_amount":               paid_amount_pe,
                "source_exchange_rate":      fx_rate_pe,
                "received_amount":           bank_amt,
                "reference_no":              ref_no,
                "reference_date":            pay_date,
                "references": [{
                    "reference_doctype": inv_type,
                    "reference_name":    inv_name,
                    "allocated_amount":  paid_amount_pe,
                }],
            }
            created = erp.create("Payment Entry", pe_data)
            return {
                "success": True,
                "message": f"Payment Entry created: {created.get('name')}",
                "data": {"name": created.get("name"), "docstatus": created.get("docstatus", 0)},
            }

        elif name == "get_payment_forex_loss":
            pe_name = args["payment_entry"]
            doc = erp.get("Payment Entry", pe_name)
            if not doc:
                return {"success": False, "error": f"Payment Entry {pe_name} not found"}
            # Realized FX gain/loss lives in the deductions child table,
            # flagged is_exchange_gain_loss. Present in draft and submitted PEs.
            net = round(sum(float(x.get("amount") or 0)
                            for x in (doc.get("deductions") or [])
                            if x.get("is_exchange_gain_loss")), 2)
            return {
                "success": True,
                "payment_entry": pe_name,
                "forex_loss": net,                       # +ve = loss, -ve = gain
                "result": "loss" if net > 0 else ("gain" if net < 0 else "none"),
                "amount": abs(net),
                "currency": "MYR",
            }

        elif name == "forex_loss_summary":
            from datetime import date, datetime, timedelta
            end   = args.get("end_date") or str(date.today())
            start = args.get("start_date") or str(date.fromisoformat(end) - timedelta(days=30))
            group_by = (args.get("group_by") or "day").lower()

            rows = erp.list(
                doctype="GL Entry",
                filters=[["account", "like", "%Exchange Gain%"],
                         ["posting_date", ">=", start], ["posting_date", "<=", end]],
                fields=["posting_date", "voucher_no", "party", "debit", "credit"],
                limit=2000,
            ).get("data", [])

            def _bucket(r):
                d = r.get("posting_date") or ""
                if group_by == "month":
                    return d[:7]
                if group_by == "week":
                    try:
                        dt = datetime.fromisoformat(str(d)).date()
                        return f"W/C {(dt - timedelta(days=dt.weekday())).isoformat()}"
                    except Exception:
                        return d
                if group_by == "party":
                    return r.get("party") or "—"
                return d

            agg, total = {}, 0.0
            for r in rows:
                net = float(r.get("debit") or 0) - float(r.get("credit") or 0)
                b = _bucket(r)
                agg[b] = round(agg.get(b, 0) + net, 2)
                total += net
            total = round(total, 2)
            labels = sorted(agg.keys())
            chart = {
                "title": f"Forex gain/loss by {group_by} ({start} to {end})",
                "labels": labels,
                "datasets": [{"label": "Net forex (MYR, +loss / -gain)",
                              "values": [agg[k] for k in labels], "color": "#ef4444"}],
                "type": "bar",
                "currency": "MYR",
            }
            return {
                "success": True, "_type": "chart", "chart": chart,
                "summary": {
                    "start": start, "end": end, "group_by": group_by,
                    "total_forex_loss": total,
                    "result": "loss" if total > 0 else ("gain" if total < 0 else "none"),
                    "entries": len(rows), "by_bucket": agg,
                },
            }

        elif name == "generate_reconciliation_report":
            pe_name = args["payment_entry"]
            doc = erp.get("Payment Entry", pe_name)
            if not doc:
                return {"success": False, "error": f"Payment Entry {pe_name} not found"}
            ref = (doc.get("references") or [{}])[0]
            losses = round(sum(float(x.get("amount") or 0)
                               for x in (doc.get("deductions") or [])
                               if x.get("is_exchange_gain_loss")), 2)
            data = {
                "payment_entry":   doc.get("name"),
                "posting_date":    doc.get("posting_date"),
                "submitted":       doc.get("docstatus") == 1,
                "party_type":      doc.get("party_type"),
                "party":           doc.get("party"),
                "invoice":         ref.get("reference_name"),
                "invoice_type":    ref.get("reference_doctype"),
                "allocated":       ref.get("allocated_amount"),
                "paid_amount":     doc.get("paid_amount"),
                "paid_currency":   doc.get("paid_from_account_currency") or doc.get("paid_to_account_currency"),
                "received_myr":    doc.get("received_amount") if doc.get("payment_type") == "Receive" else doc.get("base_paid_amount"),
                "exchange_rate":   doc.get("source_exchange_rate"),
                "expected_myr":    doc.get("base_paid_amount"),
                "losses":          losses,
                "reference_no":    doc.get("reference_no"),
                "mode_of_payment": doc.get("mode_of_payment"),
                "confidence":      args.get("confidence"),
                "match_method":    args.get("match_method"),
            }
            return {"success": True, "_type": "recon_report", "data": data,
                    "message": f"Reconciliation report ready for {pe_name}."}

        elif name == "generate_discrepancy_summary":
            data = {
                "payer":            args.get("payer"),
                "currency":         args.get("currency"),
                "amount":           args.get("amount"),
                "reference":        args.get("reference"),
                "payment_date":     args.get("payment_date"),
                "status":           args.get("status"),
                "reason":           args.get("reason"),
                "closest_invoice":  args.get("closest_invoice"),
                "confidence":       args.get("confidence"),
                "suggested_action": args.get("suggested_action"),
            }
            return {"success": True, "_type": "discrepancy", "data": data,
                    "message": f"Discrepancy summary ready for {args.get('payer','payment')}."}

        elif name == "three_way_reconcile":
            from forex import get_rate
            from bank_statement_parser import fetch_sheet

            amount       = float(args["amount"])
            currency     = args["currency"].upper()
            customer     = args.get("customer", "")
            reference    = args.get("reference", "")
            payment_date = args.get("payment_date")
            bank_month   = args["bank_month"]
            url          = args.get("sheet_url") or config.BANK_STATEMENT_SHEET_URL
            local_cur    = "MYR"

            result = {
                "proof":    {"amount": amount, "currency": currency, "customer": customer,
                             "reference": reference, "date": payment_date},
                "invoice":  None,
                "bank":     None,
                "fx":       None,
                "status":   "UNMATCHED",
                "diff_pct": None,
                "confidence": 0,
                "needs_review": True,
                "match_method": None,
                "ready_for_payment_entry": False,
                "suggested_payment_entry": None,
            }

            # ── 1. Forex conversion ──────────────────────────────────────────
            if currency != local_cur:
                fx = get_rate(currency, local_cur, payment_date)
                rate       = fx.get("rate", 1.0) if fx.get("success") else 1.0
                amount_myr = round(amount * rate, 2)
                result["fx"] = {"from": currency, "to": local_cur,
                                "rate": rate, "date": fx.get("date"),
                                "converted": amount_myr}
            else:
                rate       = 1.0
                amount_myr = amount
                result["fx"] = {"from": local_cur, "to": local_cur, "rate": 1.0,
                                "date": payment_date, "converted": amount_myr}

            # ── 2. Gather candidates (invoices + bank transactions) ──────────
            INV_TOL, BANK_TOL, RECON_TOL, NAME_TOL = 0.10, 0.05, 0.01, 0.6

            invoice_name = args.get("invoice_name")
            invoice_type = args.get("invoice_type", "Sales Invoice")

            def _to_local(amt, cur):
                amt = float(amt or 0)
                if cur and cur != local_cur:
                    fx2 = get_rate(cur, local_cur, payment_date)
                    if fx2.get("success"):
                        return round(amt * fx2.get("rate", 1), 2)
                return amt

            candidates = []
            if invoice_name:
                # Explicit override (still validated below) — fetch the full doc.
                party_field = "customer" if invoice_type == "Sales Invoice" else "supplier"
                ref_field   = "po_no" if invoice_type == "Sales Invoice" else "bill_no"
                doc = erp.get(invoice_type, invoice_name)
                if doc:
                    doc.update({"_doctype": invoice_type, "_party_field": party_field,
                                "_ref_field": ref_field,
                                "_local_amount": _to_local(doc.get("outstanding_amount") or doc.get("grand_total"),
                                                           doc.get("currency", local_cur))})
                    candidates.append(doc)
            else:
                for inv_type, party_field, ref_field in [
                    ("Sales Invoice", "customer", "po_no"),
                    ("Purchase Invoice", "supplier", "bill_no"),
                ]:
                    try:
                        docs = erp.list(
                            doctype=inv_type,
                            filters=[["docstatus", "=", 1], ["outstanding_amount", ">", 0]],
                            fields=["name", "currency", "grand_total", "outstanding_amount",
                                    party_field, ref_field],
                            limit=50,
                        ).get("data", [])
                    except Exception:
                        docs = []
                    for d in docs:
                        d.update({"_doctype": inv_type, "_party_field": party_field,
                                  "_ref_field": ref_field,
                                  "_local_amount": _to_local(d.get("outstanding_amount") or d.get("grand_total"),
                                                             d.get("currency", local_cur))})
                        candidates.append(d)

            txns = []
            if url:
                try:
                    txns = fetch_sheet(url, bank_month)
                except Exception as e:
                    result["bank_error"] = str(e)

            # ── 3. Match invoice: reference → name+amount → amount-only ──────
            inv_found, inv_method, inv_conf = None, None, 0
            for iv in candidates:
                if ref_contains(reference, iv.get("name"), iv.get(iv["_ref_field"])):
                    inv_found, inv_method, inv_conf = iv, "reference", 100
                    break
            if inv_found is None:
                best, best_score = None, 0.0
                for iv in candidates:
                    sim = name_similarity(customer, iv.get(iv["_party_field"]))
                    amt_ok, amt_sc = amount_score(amount_myr, iv["_local_amount"], INV_TOL)
                    if sim >= NAME_TOL and amt_ok and (sim * 0.4 + amt_sc * 0.6) > best_score:
                        best, best_score = iv, sim * 0.4 + amt_sc * 0.6
                if best is not None:
                    inv_found, inv_method, inv_conf = best, "name+amount", round(best_score * 100)
            if inv_found is None:
                best, best_sc = None, 0.0
                for iv in candidates:
                    amt_ok, amt_sc = amount_score(amount_myr, iv["_local_amount"], INV_TOL)
                    if amt_ok and amt_sc > best_sc:
                        best, best_sc = iv, amt_sc
                if best is not None:
                    inv_found, inv_method, inv_conf = best, "amount", min(50, round(best_sc * 50))

            # Enrich list-matched invoice with full doc (PE needs debit_to/credit_to).
            if inv_found is not None and "debit_to" not in inv_found and "credit_to" not in inv_found:
                full = erp.get(inv_found["_doctype"], inv_found["name"])
                if full:
                    meta = {k: inv_found[k] for k in
                            ("_doctype", "_party_field", "_ref_field", "_local_amount")}
                    inv_found = {**full, **meta}

            # ── 4. Match bank: reference → amount ────────────────────────────
            bank_found, bank_method = None, None
            for t in txns:
                if ref_contains(reference, t.get("reference"), t.get("description")):
                    bank_found, bank_method = t, "reference"
                    break
            if bank_found is None:
                best, best_sc = None, 0.0
                for t in txns:
                    amt_ok, amt_sc = amount_score(amount_myr, float(t.get("amount", 0)), BANK_TOL)
                    if amt_ok and amt_sc > best_sc:
                        best, best_sc = t, amt_sc
                if best is not None:
                    bank_found, bank_method = best, "amount"

            # ── 5. Populate sides ────────────────────────────────────────────
            if inv_found is not None:
                if float(inv_found.get("outstanding_amount") or 0) == 0:
                    result["invoice"] = {
                        "name": inv_found["name"], "doctype": inv_found["_doctype"],
                        "currency": inv_found.get("currency"), "amount": inv_found.get("grand_total"),
                        "party": inv_found.get(inv_found["_party_field"]), "local_amount": 0,
                    }
                    result.update({"status": "ALREADY_PAID", "confidence": inv_conf or 90,
                                   "needs_review": False, "match_method": inv_method,
                                   "_type": "three_way"})
                    return {"success": True, **result}
                result["invoice"] = {
                    "name": inv_found["name"], "doctype": inv_found["_doctype"],
                    "currency": inv_found.get("currency"),
                    "amount": inv_found.get("outstanding_amount") or inv_found.get("grand_total"),
                    "party": inv_found.get(inv_found["_party_field"]),
                    "local_amount": inv_found["_local_amount"],
                }
            if bank_found is not None:
                result["bank"] = bank_found

            # ── 6. Three-way status + confidence ─────────────────────────────
            has_inv, has_bank = inv_found is not None, bank_found is not None
            result["match_method"] = inv_method or bank_method

            if has_inv and has_bank:
                bank_amt = float(bank_found.get("amount", 0))
                diff_pct = round((bank_amt - amount_myr) / amount_myr * 100, 2) if amount_myr else 0
                result["diff_pct"] = diff_pct
                inv_local = inv_found["_local_amount"]
                inv_diff  = abs(inv_local - amount_myr) / amount_myr if amount_myr else 1.0
                party_sim = name_similarity(customer, inv_found.get(inv_found["_party_field"]))
                if inv_method == "reference":
                    party_sim = max(party_sim, 0.85)

                if inv_method == "reference":
                    confidence = 100
                elif inv_method == "name+amount":
                    confidence = round(party_sim * 40 + (1 - min(inv_diff, INV_TOL) / INV_TOL) * 60)
                else:  # amount-only fallback
                    confidence = min(50, round((1 - min(inv_diff, INV_TOL) / INV_TOL) * 50))
                result["confidence"] = confidence

                party_ok = party_sim >= NAME_TOL
                bank_ok  = abs(diff_pct) <= RECON_TOL * 100
                inv_ok   = inv_diff <= RECON_TOL

                # GUARD: a matching bank amount is NOT enough on its own — the
                # invoice party and the invoice amount must also agree, else the
                # case is flagged for human review instead of auto-reconciled.
                if party_ok and bank_ok and inv_ok and inv_method != "amount":
                    result["status"] = "RECONCILED"
                    result["needs_review"] = False
                    result["ready_for_payment_entry"] = True
                    party_field = inv_found["_party_field"]
                    is_customer = party_field == "customer"

                    # paid_from = receivable/payable GL account on the invoice
                    paid_from = inv_found.get("debit_to") or inv_found.get("credit_to", "")

                    # paid_to = company bank account (first Bank-type account found)
                    paid_to = ""
                    bank_account_doc = ""
                    try:
                        bank_accts = erp.list(
                            "Bank Account",
                            filters=[["company", "=", config.ERPNEXT_COMPANY],
                                     ["is_default", "=", 1]],
                            fields=["name", "account"],
                            limit=1,
                        ).get("data", [])
                        if not bank_accts:
                            bank_accts = erp.list(
                                "Bank Account",
                                filters=[["company", "=", config.ERPNEXT_COMPANY]],
                                fields=["name", "account"],
                                limit=1,
                            ).get("data", [])
                        if bank_accts:
                            paid_to = bank_accts[0].get("account", "")
                            bank_account_doc = bank_accts[0].get("name", "")
                    except Exception:
                        pass

                    inv_currency    = inv_found.get("currency", local_cur)
                    inv_outstanding = float(inv_found.get("outstanding_amount") or inv_found.get("grand_total") or 0)

                    # paid_amount must be in paid_from account currency (= invoice currency)
                    # received_amount is always the local MYR amount received in bank
                    if inv_currency != local_cur:
                        # e.g. invoice is SGD, bank received MYR — use invoice outstanding as paid_amount
                        paid_amount_pe = inv_outstanding
                        fx2 = get_rate(inv_currency, local_cur, payment_date)
                        fx_rate_pe = fx2.get("rate", rate) if fx2.get("success") else rate
                    else:
                        # both in local currency
                        paid_amount_pe = bank_amt
                        fx_rate_pe = 1.0

                    result["suggested_payment_entry"] = {
                        "doctype":                   "Payment Entry",
                        "payment_type":              "Receive" if is_customer else "Pay",
                        "posting_date":              payment_date or str(__import__("datetime").date.today()),
                        "company":                   config.ERPNEXT_COMPANY,
                        "mode_of_payment":           "Bank Transfer",
                        "party_type":                "Customer" if is_customer else "Supplier",
                        "party":                     result["invoice"]["party"],
                        "paid_from":                 paid_from,
                        "paid_from_account_currency": inv_currency,
                        "bank_account":              bank_account_doc,
                        "paid_to":                   paid_to,
                        "paid_to_account_currency":  local_cur,
                        "paid_amount":               paid_amount_pe,
                        "source_exchange_rate":      fx_rate_pe,
                        "received_amount":           bank_amt,
                        "reference_no":              reference or bank_found.get("reference", ""),
                        "reference_date":            payment_date or bank_found.get("date", ""),
                        "references": [{
                            "reference_doctype": inv_found["_doctype"],
                            "reference_name":    inv_found["name"],
                            "allocated_amount":  paid_amount_pe,
                        }],
                    }
                else:
                    # Bank amount matched but invoice party/amount didn't fully
                    # check out (or it was an amount-only match) — needs a human.
                    result["status"] = "PARTIAL"
                    result["needs_review"] = True
            elif has_inv and not has_bank:
                result["status"] = "PENDING"
                result["diff_pct"] = None
                result["confidence"] = inv_conf if inv_method == "reference" else min(inv_conf, 70)
                result["needs_review"] = True
            elif not has_inv and has_bank:
                result["status"] = "UNMATCHED_INVOICE"
                result["confidence"] = 70 if bank_method == "reference" else 40
                result["needs_review"] = True
            else:
                result["status"] = "UNMATCHED"
                result["confidence"] = 0
                result["needs_review"] = True

            result["_type"] = "three_way"
            return {"success": True, **result}

        elif name == "fetch_bank_statement":
            from bank_statement_parser import fetch_sheet
            url = args.get("sheet_url") or config.BANK_STATEMENT_SHEET_URL
            if not url:
                return {"success": False, "error": "No Google Sheet URL configured. Set BANK_STATEMENT_SHEET_URL in config.py or pass sheet_url."}
            month = args["month"]
            try:
                txns = fetch_sheet(url, month)
                return {"success": True, "month": month, "count": len(txns), "transactions": txns}
            except ValueError as e:
                return {"success": False, "error": str(e)}

        elif name == "reconcile_transactions":
            from forex import get_rate
            transactions = args.get("transactions", [])
            invoice_types = []
            inv_type_arg = args.get("invoice_type", "both")
            if inv_type_arg in ("Sales Invoice", "both"):
                invoice_types.append("Sales Invoice")
            if inv_type_arg in ("Purchase Invoice", "both"):
                invoice_types.append("Purchase Invoice")

            results = []
            for txn in transactions:
                row = {**txn, "matched_invoice": None, "invoice_currency": None,
                       "invoice_amount": None, "expected_local": None,
                       "diff_pct": None, "status": "UNMATCHED", "note": ""}

                def _inv_fields(inv_type):
                    party = "customer" if inv_type == "Sales Invoice" else "supplier"
                    return ["name", "currency", "grand_total", "outstanding_amount", party]

                # 1. Try reference match first
                matched_doc = None
                for inv_type in invoice_types:
                    if txn.get("reference"):
                        docs = erp.list(
                            doctype=inv_type,
                            filters=[["name", "like", f"%{txn['reference']}%"],
                                     ["docstatus", "=", 1]],
                            fields=_inv_fields(inv_type),
                            limit=1,
                        ).get("data", [])
                        if docs:
                            matched_doc = {**docs[0], "_doctype": inv_type}
                            break

                # 2. Fallback: amount match within 10% on outstanding invoices
                if not matched_doc:
                    for inv_type in invoice_types:
                        docs = erp.list(
                            doctype=inv_type,
                            filters=[["docstatus", "=", 1],
                                     ["outstanding_amount", ">", 0]],
                            fields=_inv_fields(inv_type),
                            limit=50,
                        ).get("data", [])
                        for doc in docs:
                            inv_amt = float(doc.get("outstanding_amount") or doc.get("grand_total") or 0)
                            if inv_amt == 0:
                                continue
                            inv_currency = doc.get("currency", "MYR")
                            # Convert invoice to local currency for comparison
                            if inv_currency != txn.get("currency", "MYR"):
                                fx = get_rate(inv_currency, txn.get("currency", "MYR"), txn.get("date"))
                                rate = fx.get("rate", 1) if fx.get("success") else 1
                                expected = inv_amt * rate
                            else:
                                expected = inv_amt
                            diff = abs(txn["amount"] - expected) / expected if expected else 1
                            if diff <= 0.10:  # within 10%
                                matched_doc = {**doc, "_doctype": inv_type}
                                break
                        if matched_doc:
                            break

                # 3. Compute reconciliation result
                if matched_doc:
                    inv_currency = matched_doc.get("currency", "MYR")
                    inv_amount = float(matched_doc.get("outstanding_amount") or matched_doc.get("grand_total") or 0)
                    local_currency = txn.get("currency", "MYR")

                    if inv_currency != local_currency:
                        fx = get_rate(inv_currency, local_currency, txn.get("date"))
                        rate = fx.get("rate", 1) if fx.get("success") else 1
                        expected_local = round(inv_amount * rate, 2)
                    else:
                        rate = 1.0
                        expected_local = inv_amount

                    diff_pct = (txn["amount"] - expected_local) / expected_local * 100 if expected_local else 0

                    if abs(diff_pct) <= 1.0:
                        status = "MATCHED"
                    elif abs(diff_pct) <= 5.0:
                        status = "PARTIAL"
                    else:
                        status = "UNMATCHED"

                    party = matched_doc.get("customer") or matched_doc.get("supplier") or ""
                    row.update({
                        "matched_invoice": matched_doc.get("name"),
                        "invoice_currency": inv_currency,
                        "invoice_amount": inv_amount,
                        "expected_local": expected_local,
                        "diff_pct": round(diff_pct, 2),
                        "status": status,
                        "note": party,
                    })
                else:
                    row["note"] = "No matching invoice found"

                results.append(row)

            summary = {
                "total": len(results),
                "matched":   sum(1 for r in results if r["status"] == "MATCHED"),
                "partial":   sum(1 for r in results if r["status"] == "PARTIAL"),
                "unmatched": sum(1 for r in results if r["status"] == "UNMATCHED"),
            }
            return {"success": True, "summary": summary, "results": results,
                    "_type": "reconciliation"}

        elif name == "get_forex_rate":
            from forex import get_rate, convert
            amount = args.get("amount")
            if amount is not None:
                return convert(float(amount), args["from_currency"], args["to_currency"], args.get("date"))
            return get_rate(args["from_currency"], args["to_currency"], args.get("date"))

        # ── Write operations ────────────────────────────────────────
        elif name == "erpnext_create":
            doc = erp.create(args["doctype"], args["data"])
            return {
                "success": True,
                "message": f"Created {args['doctype']}: {doc.get('name', 'unknown')}",
                "data": {"name": doc.get("name"), "docstatus": doc.get("docstatus", 0)},
            }

        elif name == "erpnext_update":
            # Fetch existing row names so we can identify truly new child rows
            existing = erp.get(args["doctype"], args["name"])
            existing_row_names = {
                item.get("name")
                for item in existing.get("items", [])
                if item.get("name")
            }
            # Strip hallucinated "name" from new items (not in existing doc)
            data = args["data"]
            for item in data.get("items", []):
                if item.get("name") and item["name"] not in existing_row_names:
                    del item["name"]
            doc = erp.update(args["doctype"], args["name"], data)
            return {
                "success": True,
                "message": f"Updated {args['doctype']}: {args['name']}",
                "data": {"name": doc.get("name"), "docstatus": doc.get("docstatus", 0)},
            }

        # ── Chart tools ─────────────────────────────────────────────────
        elif name == "erpnext_sales_chart":
            from collections import defaultdict
            group_by   = args.get("group_by", "customer")
            limit      = int(args.get("limit", 10))
            color_arg  = args.get("color")
            chart_type = args.get("chart_type")  # user override
            filters    = [["docstatus", "=", 1]]
            if args.get("date_from"):
                filters.append(["posting_date", ">=", args["date_from"]])
            if args.get("date_to"):
                filters.append(["posting_date", "<=", args["date_to"]])

            if group_by == "month":
                invoices = erp.list("Sales Invoice", filters=filters,
                                    fields=["posting_date", "grand_total"], limit=1000)
                monthly = defaultdict(float)
                for inv in invoices.get("data", []):
                    month = str(inv.get("posting_date", ""))[:7]
                    monthly[month] += float(inv.get("grand_total") or 0)
                sorted_months = sorted(monthly.items())
                return {
                    "success": True, "_type": "chart",
                    "chart": {
                        "type": chart_type or "line",
                        "title": "Monthly Sales Revenue",
                        "labels": [m for m, _ in sorted_months],
                        "datasets": [{"label": "Revenue",
                                      "values": [round(v, 2) for _, v in sorted_months],
                                      "color": color_arg or "#60a5fa"}],
                        "currency": "MYR",
                    }
                }

            elif group_by == "item":
                invoices = erp.list("Sales Invoice", filters=filters, fields=["name"], limit=200)
                item_totals = defaultdict(float)
                item_names  = {}
                for inv in invoices.get("data", []):
                    doc = erp.get("Sales Invoice", inv["name"])
                    for item in doc.get("items", []):
                        code = item.get("item_code", "Unknown")
                        item_totals[code] += float(item.get("amount") or 0)
                        if code not in item_names:
                            item_names[code] = item.get("item_name", code)
                top = sorted(item_totals.items(), key=lambda x: -x[1])[:limit]
                return {
                    "success": True, "_type": "chart",
                    "chart": {
                        "type": chart_type or "bar",
                        "title": f"Top {limit} Items by Sales Revenue",
                        "labels": [item_names.get(k, k) for k, _ in top],
                        "datasets": [{"label": "Revenue",
                                      "values": [round(v, 2) for _, v in top],
                                      "color": color_arg or "#34d399"}],
                        "currency": "MYR",
                    }
                }

            else:  # customer
                result = erp.list("Sales Invoice", filters=filters,
                                  group_by="customer", sum_field="grand_total")
                top = result.get("data", [])[:limit]
                return {
                    "success": True, "_type": "chart",
                    "chart": {
                        "type": chart_type or "bar",
                        "title": f"Top {limit} Customers by Revenue",
                        "labels": [r["group"] for r in top],
                        "datasets": [{"label": "Revenue",
                                      "values": [round(r["grand_total"], 2) for r in top],
                                      "color": color_arg or "#818cf8"}],
                        "currency": "MYR",
                    }
                }

        elif name == "erpnext_stock_chart":
            from collections import defaultdict
            limit      = int(args.get("limit", 20))
            min_qty    = args.get("min_qty", 1)
            color_arg  = args.get("color")
            chart_type = args.get("chart_type", "horizontal-bar")
            filters    = [["actual_qty", ">=", min_qty]]
            if args.get("item_group"):
                filters.append(["item_group", "=", args["item_group"]])
            if args.get("warehouse"):
                filters.append(["warehouse", "=", args["warehouse"]])

            bins = erp.list("Bin", filters=filters,
                            fields=["item_code", "actual_qty"], limit=500)
            totals = defaultdict(float)
            for b in bins.get("data", []):
                totals[b["item_code"]] += float(b.get("actual_qty") or 0)
            top = sorted(totals.items(), key=lambda x: -x[1])[:limit]
            return {
                "success": True, "_type": "chart",
                "chart": {
                    "type": chart_type,
                    "title": f"Top {limit} Items by Stock Level",
                    "labels": [k for k, _ in top],
                    "datasets": [{"label": "Qty on Hand",
                                  "values": [round(v, 2) for _, v in top],
                                  "color": color_arg or "#fb923c"}],
                }
            }

        else:
            return {"success": False, "error": f"Unknown tool: {name}"}

    except Exception as e:
        return {"success": False, "error": str(e)}
