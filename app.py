"""
Streamlit frontend — Global Treasury Agent
"""
import http.server
import json
import re
import socket
import threading
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components

import config
import auth as _auth
from db import init_db, save_message, load_messages, list_sessions, delete_session, update_session_title, create_session
from agent import LLMAdapter
from domains import get_domain_config
from erpnext_client import get_erp_adapter, get_erp_adapter_cookie
from invoice_extractor import extract_invoice, extract_payment_receipt
from bank_statement_parser import parse_csv
from tools import execute_tool, get_tools_for_domain

# ── Web Speech API component ──────────────────────────────────────────────────

@st.cache_resource
def _start_speech_server() -> int:
    component_dir = str(Path(__file__).parent / "components" / "speech_input")

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=component_dir, **kwargs)
        def log_message(self, *args):
            pass

    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    server = http.server.HTTPServer(("", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port

_speech_port = _start_speech_server()
_speech_input = components.declare_component("speech_input", url=f"http://localhost:{_speech_port}")

# ── Staff roster ──────────────────────────────────────────────────────────────

STAFF = {
    "Treasury Agent": "accounting",
}

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Global Treasury Agent",
    page_icon="💱",
    layout="wide",
)

# ── Cached resources ──────────────────────────────────────────────────────────

@st.cache_resource
def init_llm(model_key: str):
    return LLMAdapter(config.MODELS[model_key])

@st.cache_resource
def init_erp():
    return get_erp_adapter()

def get_session_erp():
    """Cookie-based adapter when logged in, else falls back to API-key adapter."""
    if st.session_state.get("erp_cookies"):
        return get_erp_adapter_cookie(
            st.session_state.erp_cookies,
            st.session_state.get("erp_csrf", ""),
        )
    return init_erp()

# ── Helpers ───────────────────────────────────────────────────────────────────

def strip_thinking(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"!\[.*?\]\(https?://[^\)]+\)", "", text)
    return text.strip()

def msg_role(msg) -> str:
    return msg["role"] if isinstance(msg, dict) else msg.role

def msg_content(msg):
    raw = msg.get("content") if isinstance(msg, dict) else msg.content
    return raw or ""

def msg_text(msg) -> str:
    """Return plain text from a message (handles multimodal list content)."""
    raw = msg_content(msg)
    if isinstance(raw, list):
        return " ".join(p["text"] for p in raw if p.get("type") == "text")
    return raw or ""

def msg_image_url(msg) -> str | None:
    """Return base64 image URL if message has an image, else None."""
    raw = msg_content(msg)
    if isinstance(raw, list):
        for p in raw:
            if p.get("type") == "image_url":
                return p["image_url"]["url"]
    return None

def has_tool_calls(msg) -> bool:
    tc = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
    return bool(tc)

# ── ERPNext document link buttons ────────────────────────────────────────────

_DOC_SLUG = {
    "SINV": ("sales-invoice",    "Sales Invoice"),
    "PINV": ("purchase-invoice", "Purchase Invoice"),
    "PAY":  ("payment-entry",    "Payment Entry"),
    "SORD": ("sales-order",      "Sales Order"),
    "PORD": ("purchase-order",   "Purchase Order"),
    "QORD": ("quotation",        "Quotation"),
    "DNO":  ("delivery-note",    "Delivery Note"),
    "RFQ":  ("request-for-quotation", "RFQ"),
}

def render_doc_buttons(text: str):
    """Find ERPNext doc names in text and render Open buttons."""
    pattern = r'\b([A-Z]{2,5}-(?:' + '|'.join(_DOC_SLUG) + r')-\d{4}-\d{5})\b'
    found = list(dict.fromkeys(re.findall(pattern, text)))  # unique, order preserved
    if not found:
        return
    cols = st.columns(min(len(found), 4))
    for i, name in enumerate(found):
        code = name.split("-")[1]
        slug, label = _DOC_SLUG.get(code, ("document", "Document"))
        url = f"{config.ERPNEXT_URL}/app/{slug}/{name}"
        cols[i % 4].link_button(f"🔗 {name}", url, use_container_width=True)

# ── Write confirmation card ───────────────────────────────────────────────────

WRITE_TOOLS = {"erpnext_create", "erpnext_update", "create_payment_entry"}

def render_confirmation_card(pending: dict, erp, conversation: list):
    name = pending["name"]
    args = pending["args"]
    tc_id = pending["tool_call_id"]

    # ── Build display rows depending on tool ────────────────────────────────
    if name == "create_payment_entry":
        icon, title = "💳", "Create Payment Entry"
        rows = [
            ("Invoice",       args.get("invoice_name", "")),
            ("Type",          args.get("invoice_type", "")),
            ("Bank Amount",   f"MYR {args.get('bank_amount', '')}"),
            ("Payment Date",  args.get("payment_date", "")),
            ("Reference No",  args.get("reference_no", "")),
        ]
    elif name == "erpnext_update":
        icon, title = "✏️", f"Update {args.get('doctype','')} · {args.get('name','')}"
        rows = [(k, v) for k, v in args.get("data", {}).items() if not isinstance(v, list)]
    else:
        doctype = args.get("doctype", "Document")
        icon, title = "➕", f"Create {doctype}"
        data = args.get("data", {})
        rows = []
        for k, v in data.items():
            if isinstance(v, list):
                rows.append((k, f"{len(v)} item(s)"))
            else:
                rows.append((k, v))

    # ── Card UI ──────────────────────────────────────────────────────────────
    st.markdown(
        f"""<div style="border:1px solid #e2e8f0; border-radius:12px; padding:16px 20px;
                        background:#f8fafc; margin-bottom:12px;">
            <div style="font-size:1.05rem; font-weight:600; margin-bottom:12px;">
                {icon} {title}
            </div>""",
        unsafe_allow_html=True,
    )
    for field, val in rows:
        label = field.replace("_", " ").title()
        st.markdown(
            f"""<div style="display:flex; justify-content:space-between;
                            padding:6px 0; border-bottom:1px solid #e2e8f0;">
                    <span style="color:#64748b; font-size:0.85rem;">{label}</span>
                    <span style="font-weight:500; font-size:0.9rem;">{val}</span>
                </div>""",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

    col_yes, col_no, _ = st.columns([1, 1, 4])
    confirmed = col_yes.button("✅ Confirm", key=f"confirm_{tc_id}", type="primary")
    cancelled = col_no.button("✕ Cancel",   key=f"cancel_{tc_id}")

    if confirmed:
        result = execute_tool(name, args, erp)
        if name == "create_payment_entry" and result.get("success"):
            pe_name = result.get("data", {}).get("name")
            if pe_name:
                # Attach the payment proof to the freshly created Payment Entry.
                if st.session_state.get("proof_bytes") and st.session_state.get("proof_filename"):
                    try:
                        erp.upload_file(st.session_state.proof_bytes,
                                        st.session_state.proof_filename, "Payment Entry", pe_name)
                    except Exception:
                        pass
                # Auto-generate the Reconciliation Report (don't rely on the model to call it).
                tw = st.session_state.get("three_way_result") or {}
                rep = execute_tool("generate_reconciliation_report", {
                    "payment_entry": pe_name,
                    "confidence":    tw.get("confidence"),
                    "match_method":  tw.get("match_method"),
                }, erp)
                if rep.get("_type"):
                    st.session_state.artifacts.append(rep)
        conversation.append({
            "role": "tool", "tool_call_id": tc_id,
            "content": json.dumps(result, ensure_ascii=False),
        })
        st.session_state.pending_write = None
        st.session_state.resume_agent = True
        st.rerun()

    if cancelled:
        conversation.append({
            "role": "tool", "tool_call_id": tc_id,
            "content": json.dumps({"success": False, "error": "User cancelled the operation."}),
        })
        st.session_state.pending_write = None
        st.session_state.resume_agent = True
        st.rerun()

# ── Payment proof review form ─────────────────────────────────────────────────

def render_payment_proof_review():
    data = st.session_state.proof_data
    filename = st.session_state.proof_filename or "document"
    doc_type = st.session_state.proof_type  # "invoice" or "receipt"

    label = "Invoice" if doc_type == "invoice" else "Payment Receipt"
    st.subheader(f"📄 Review Extracted {label}")
    st.caption(f"Source: {filename}")

    with st.form("proof_review_form"):
        if doc_type == "invoice":
            col1, col2, col3 = st.columns(3)
            supplier  = col1.text_input("Supplier",    value=data.get("supplier_name") or "")
            inv_no    = col2.text_input("Invoice No.", value=data.get("invoice_no") or "")
            inv_date  = col3.text_input("Date",        value=data.get("invoice_date") or "")

            col4, col5 = st.columns(2)
            currency  = col4.text_input("Currency", value=data.get("currency") or "USD")
            total     = col5.number_input("Total", value=float(data.get("total") or 0), format="%.2f")

        else:  # receipt
            col1, col2 = st.columns(2)
            sender    = col1.text_input("Sender",      value=data.get("sender_name") or "")
            receiver  = col2.text_input("Receiver",    value=data.get("receiver_name") or "")

            col3, col4 = st.columns(2)
            pay_date  = col3.text_input("Payment Date", value=data.get("payment_date") or "")
            ref_no    = col4.text_input("Reference No.", value=data.get("reference_no") or "")

            col5, col6, col7 = st.columns(3)
            amount    = col5.number_input("Amount Paid",      value=float(data.get("amount_paid") or 0), format="%.2f")
            currency  = col6.text_input("Currency",           value=data.get("currency") or "MYR")
            orig_amt  = col7.number_input("Original Amount",  value=float(data.get("original_amount") or 0), format="%.2f")

            col8, col9 = st.columns(2)
            orig_curr = col8.text_input("Original Currency", value=data.get("original_currency") or "")
            fx_rate   = col9.number_input("Exchange Rate",   value=float(data.get("exchange_rate") or 0), format="%.6f")

        st.divider()
        col_send, col_discard, _ = st.columns([1, 1, 4])
        submitted = col_send.form_submit_button("Send to Treasury Agent", type="primary")
        discarded = col_discard.form_submit_button("Discard")

    if submitted:
        if doc_type == "invoice":
            prompt = (
                f"I have extracted an invoice from '{filename}'. "
                f"Supplier: {supplier}, Invoice No: {inv_no}, Date: {inv_date}, "
                f"Currency: {currency}, Total: {total}. "
                f"Please find the matching ERPNext invoice and check if it is outstanding. "
                f"If it involves a foreign currency, reconcile the expected local amount."
            )
        else:
            prompt = (
                f"I have a payment receipt from '{filename}'. "
                f"Sender: {sender}, Receiver: {receiver}, Date: {pay_date}, "
                f"Reference: {ref_no}, Amount: {currency} {amount}. "
                + (f"Original amount: {orig_curr} {orig_amt}. " if orig_amt else "")
                + f"Please search ERPNext for the matching outstanding invoice for this sender, "
                + f"then run three_way_reconcile with the found invoice_name and invoice_type. "
                + f"Use bank month May2026."
            )

        conv = st.session_state.conversations.setdefault(
            "accounting",
            [{"role": "system", "content": get_domain_config("accounting")["system_prompt"]}],
        )
        conv.append({"role": "user", "content": prompt})
        st.session_state.proof_data     = None
        st.session_state.proof_filename = None
        st.session_state.proof_type     = None
        st.session_state._switch_to_staff = "Treasury Agent"
        st.session_state.resume_agent = True
        st.rerun()

    if discarded:
        st.session_state.proof_data     = None
        st.session_state.proof_filename = None
        st.session_state.proof_type     = None
        st.rerun()

# ── Three-way reconciliation card ────────────────────────────────────────────

STATUS_META = {
    "RECONCILED":        ("✅", "success",  "All three match — ready to post"),
    "PARTIAL":           ("⚠️", "warning",  "Amounts differ > 1% — review before posting"),
    "PENDING":           ("🕐", "info",     "Invoice matched but payment not yet in bank"),
    "UNMATCHED_INVOICE": ("❓", "warning",  "Bank entry found but no matching invoice"),
    "UNMATCHED":         ("❌", "error",    "No matching invoice or bank entry found"),
    "ALREADY_PAID":      ("💚", "success",  "Invoice already fully paid — no action needed"),
}

def render_three_way_card(result: dict, erp, conversation: list):
    status = result.get("status", "UNMATCHED")
    icon, stype, note = STATUS_META.get(status, ("❓", "info", ""))

    st.subheader(f"{icon} {status} — {note}")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("**💳 Payment Proof**")
        proof = result.get("proof", {})
        st.metric("Amount", f"{proof.get('currency','')} {proof.get('amount','')}")
        st.caption(f"Customer: {proof.get('customer','—')}")
        st.caption(f"Date: {proof.get('date','—')}")
        st.caption(f"Ref: {proof.get('reference','—')}")

    with col2:
        st.markdown("**📄 ERPNext Invoice**")
        inv = result.get("invoice")
        if inv:
            st.metric("Amount", f"{inv.get('currency','')} {inv.get('amount','')}")
            st.caption(f"Invoice: {inv.get('name','—')}")
            st.caption(f"Party: {inv.get('party','—')}")
            fx = result.get("fx", {})
            if fx and fx.get("rate", 1) != 1:
                st.caption(f"FX: {fx['from']}→{fx['to']} @ {fx['rate']}")
                st.caption(f"= MYR {fx.get('converted','')}")
        else:
            st.warning("No matching invoice found")

    with col3:
        st.markdown("**🏦 Bank Statement**")
        bank = result.get("bank")
        if bank:
            st.metric("Amount", f"MYR {bank.get('amount','')}")
            st.caption(f"Date: {bank.get('date','—')}")
            st.caption(f"Ref: {bank.get('reference','—')}")
            st.caption(f"Desc: {bank.get('description','—')[:40]}")
            diff = result.get("diff_pct")
            if diff is not None:
                color = "green" if abs(diff) <= 1 else "orange"
                st.markdown(f"Diff: :{color}[{diff:+.2f}%]")
        else:
            st.warning("Not found in bank statement")

    if result.get("ready_for_payment_entry"):
        st.divider()
        pe = result["suggested_payment_entry"]
        st.markdown(f"**Ready to post:** Payment Entry for **{pe.get('party')}** — "
                    f"Received MYR {pe.get('received_amount')}")
        if st.button("✅ Create Payment Entry", type="primary", key="create_pe"):
            tool_result = execute_tool("erpnext_create", {
                "doctype": "Payment Entry",
                "data": pe,
            }, erp)
            if tool_result.get("success"):
                st.success(f"Created: {tool_result['data']['name']}")
                st.session_state.three_way_result = None
                conversation.append({
                    "role": "user",
                    "content": f"Payment Entry created: {tool_result['data']['name']}. The invoice is now paid."
                })
                st.session_state.resume_agent = True
                st.rerun()
            else:
                st.error(tool_result.get("error", "Failed"))

# ── Per-photo payment processing (agent-driven, one at a time) ────────────────

def _photo_instruction(name: str, data: dict, bank_month: str, sheet_url: str) -> str:
    return (
        f"A payment proof '{name}' was uploaded (image attached). "
        f"Extracted data: {json.dumps(data, ensure_ascii=False)}.\n"
        f"Reconcile THIS payment now:\n"
        f"1. Call three_way_reconcile with the extracted amount/currency, the counterparty "
        f"name (use sender_name if we received money, receiver_name if we paid a supplier), "
        f"reference, payment_date, bank_month='{bank_month}', sheet_url='{sheet_url}'.\n"
        f"2. If status is RECONCILED with high confidence and not needs_review: create the "
        f"Payment Entry, then call get_payment_forex_loss on the new Payment Entry and tell me "
        f"the realized forex gain/loss.\n"
        f"3. If it needs_review (PARTIAL / PENDING / UNMATCHED / low confidence): clearly explain "
        f"the problem and ask me how to proceed. Do NOT create anything.\n"
        f"Work only on this one document."
    )

def start_next_photo():
    """Pop the next queued photo, extract it, and inject it into the main conversation."""
    queue = st.session_state.batch_queue
    if not queue:
        return
    f = queue.pop(0)
    name, file_bytes = f["filename"], f["bytes"]
    st.session_state.batch_pos = st.session_state.batch_total - len(queue)

    try:
        data = extract_payment_receipt(file_bytes, name)
    except Exception as e:
        st.session_state.conversations.setdefault("accounting", []).append(
            {"role": "assistant", "content": f"💥 Couldn't read `{name}`: {e}. Click Next to continue."}
        )
        return

    # Display image (PDF → first page PNG) for the chat bubble.
    ext = name.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        import fitz as _fitz
        _doc = _fitz.open(stream=file_bytes, filetype="pdf")
        img_bytes = _doc[0].get_pixmap(matrix=_fitz.Matrix(2, 2)).tobytes("png")
        mime = "image/png"
    else:
        img_bytes, mime = file_bytes, f"image/{ext.replace('jpg', 'jpeg')}"
    import base64 as _b64
    b64 = _b64.b64encode(img_bytes).decode()

    # Proof bytes are used to attach the file to the Payment Entry on confirm.
    st.session_state.proof_bytes    = file_bytes
    st.session_state.proof_filename = name

    conv = st.session_state.conversations.setdefault(
        "accounting",
        [{"role": "system", "content": get_domain_config("accounting")["system_prompt"]}],
    )
    msg = [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        {"type": "text", "text": _photo_instruction(name, data, st.session_state.batch_bank_month,
                                                     st.session_state.batch_sheet_url)},
    ]
    conv.append({"role": "user", "content": msg})
    save_message(st.session_state.session_id, "accounting", "user", msg)
    update_session_title(st.session_state.session_id, name)
    st.session_state.resume_agent = True

# ── Chart renderer ────────────────────────────────────────────────────────────

def _render_chart(chart: dict):
    title    = chart.get("title", "")
    labels   = chart.get("labels", [])
    datasets = chart.get("datasets", [])
    ctype    = chart.get("type", "bar")
    currency = chart.get("currency", "")

    if not labels or not datasets:
        return

    values = datasets[0].get("values", [])
    color  = datasets[0].get("color", "#60a5fa")
    ylabel = datasets[0].get("label", "")
    if currency:
        ylabel = f"{ylabel} ({currency})"

    if ctype == "line":
        fig = px.line(x=labels, y=values, title=title, labels={"x": "", "y": ylabel}, markers=True)
        fig.update_traces(line_color=color)
    elif ctype in ("pie", "donut"):
        fig = px.pie(names=labels, values=values, title=title, hole=0.4 if ctype == "donut" else 0)
    elif ctype == "horizontal-bar":
        fig = px.bar(x=values, y=labels, orientation="h", title=title, labels={"x": ylabel, "y": ""})
        fig.update_traces(marker_color=color)
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
    else:
        fig = px.bar(x=labels, y=values, title=title, labels={"x": "", "y": ylabel})
        fig.update_traces(marker_color=color)

    fig.update_layout(margin={"t": 50, "b": 20}, height=400)
    st.plotly_chart(fig, use_container_width=True)

# ── PDF artifacts (reconciliation report / discrepancy summary) ───────────────

def _html_to_pdf(html: str) -> bytes:
    """Render an HTML string to PDF bytes using PyMuPDF's Story engine (no extra deps)."""
    import fitz, io
    buf = io.BytesIO()
    story = fitz.Story(html=html)
    writer = fitz.DocumentWriter(buf)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (40, 40, -40, -40)
    more = 1
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()

def _rows_html(rows: list) -> str:
    return "".join(
        f"<tr><td class='k' width='160'>{k}</td><td class='v' width='220'>{'' if v is None else v}</td></tr>"
        for k, v in rows if v not in (None, "")
    )

_PDF_CSS = """
<style>
  body { font-family: sans-serif; color: #1e293b; }
  h1 { font-size: 17pt; margin: 0 0 2pt 0; }
  .sub { color: #64748b; font-size: 9pt; margin-bottom: 10pt; }
  .badge { font-size: 10pt; font-weight: bold; padding: 2pt 6pt; border-radius: 4pt; }
  table { border-collapse: collapse; margin-top: 6pt; }
  td { padding: 5pt 8pt; border-bottom: 1px solid #e2e8f0; font-size: 10pt; vertical-align: top; }
  td.k { color: #64748b; min-width: 160pt; max-width: 160pt; }
  td.v { font-weight: 600; min-width: 220pt; }
  .foot { color: #94a3b8; font-size: 8pt; margin-top: 14pt; }
</style>
"""

def _recon_report_html(d: dict) -> str:
    fx = ""
    if d.get("paid_currency") and d.get("paid_currency") != "MYR":
        fx = f"{d.get('paid_currency')} → MYR @ {d.get('exchange_rate')}"
    rows = [
        ("Payment Entry", d.get("payment_entry")),
        ("Status", "Submitted" if d.get("submitted") else "Draft"),
        ("Posting Date", d.get("posting_date")),
        (f"{d.get('party_type','Party')}", d.get("party")),
        ("Invoice", f"{d.get('invoice')} ({d.get('invoice_type')})"),
        ("Allocated", f"{d.get('paid_currency','')} {d.get('allocated','')}"),
        ("Paid", f"{d.get('paid_currency','')} {d.get('paid_amount','')}"),
        ("FX", fx),
        ("Expected (MYR)", d.get("expected_myr")),
        ("Received in bank (MYR)", d.get("received_myr")),
        ("Losses (FX + bank charges)", f"MYR {d.get('losses')}"),
        ("Bank Reference", d.get("reference_no")),
        ("Match", f"confidence {d.get('confidence')} · {d.get('match_method')}"
                  if d.get("confidence") is not None else None),
    ]
    return f"""<html><head>{_PDF_CSS}</head><body>
        <h1>Reconciliation Report</h1>
        <div class="sub">Three-way match: payment proof &middot; ERPNext invoice &middot; bank statement</div>
        <span class="badge" style="background:#bbf7d0;color:#14532d;">RECONCILED</span>
        <table>{_rows_html(rows)}</table>
        <div class="foot">Generated by Treasury Agent &middot; figures sourced live from ERPNext Payment Entry {d.get('payment_entry')}.</div>
    </body></html>"""

def _discrepancy_html(d: dict) -> str:
    rows = [
        ("Payer", d.get("payer")),
        ("Amount", f"{d.get('currency','')} {d.get('amount','')}"),
        ("Reference", d.get("reference")),
        ("Payment Date", d.get("payment_date")),
        ("Status", d.get("status")),
        ("Closest invoice", d.get("closest_invoice")),
        ("Confidence", d.get("confidence")),
    ]
    return f"""<html><head>{_PDF_CSS}</head><body>
        <h1>Discrepancy Summary</h1>
        <div class="sub">This payment could not be auto-reconciled and needs review.</div>
        <span class="badge" style="background:#fecaca;color:#7f1d1d;">{d.get('status','NEEDS REVIEW')}</span>
        <table>{_rows_html(rows)}</table>
        <p style="font-size:10pt;"><b>Why it failed:</b><br>{d.get('reason','')}</p>
        {f"<p style='font-size:10pt;'><b>Suggested action:</b><br>{d.get('suggested_action')}</p>" if d.get('suggested_action') else ""}
        <div class="foot">Generated by Treasury Agent.</div>
    </body></html>"""

def render_artifact(art: dict, idx: int):
    """Show an artifact summary + a PDF download button."""
    data = art.get("data", {})
    if art.get("_type") == "recon_report":
        title = f"📄 Reconciliation Report — {data.get('payment_entry','')}"
        html, fname = _recon_report_html(data), f"reconciliation_{data.get('payment_entry','report')}.pdf"
    else:
        title = f"⚠️ Discrepancy Summary — {data.get('payer','')}"
        html, fname = _discrepancy_html(data), f"discrepancy_{(data.get('payer') or 'payment').split()[0]}.pdf"
    with st.container(border=True):
        st.markdown(f"**{title}**")
        try:
            pdf = _html_to_pdf(html)
            st.download_button("⬇️ Download PDF", pdf, file_name=fname,
                               mime="application/pdf", key=f"dl_artifact_{idx}")
        except Exception as e:
            st.error(f"PDF generation failed: {e}")

# ── Agent loop ────────────────────────────────────────────────────────────────

def run_agent_loop(llm, erp, conversation, domain_tools, domain_key):
    with st.chat_message("assistant"):
        answer = None
        charts = []
        produced_card = False
        total_tokens = 0
        total_time = 0.0

        with st.status("Working…", expanded=True) as status:
            for _ in range(config.MAX_TOOL_LOOPS):
                t0 = time.perf_counter()
                response = llm.chat(conversation, domain_tools)
                total_time += time.perf_counter() - t0

                usage = getattr(response, "usage", None)
                if usage:
                    total_tokens += getattr(usage, "completion_tokens", 0)

                msg = response.choices[0].message
                conversation.append(msg)

                llm_text = strip_thinking(msg.content or "")
                if llm_text:
                    status.write(f"**LLM:** {llm_text}")

                if not msg.tool_calls:
                    answer = llm_text
                    status.update(label="Done", state="complete", expanded=False)
                    break

                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    status.write(f"**Tool:** `{tc.function.name}`")

                    if tc.function.name in WRITE_TOOLS:
                        st.session_state.pending_write = {
                            "tool_call_id": tc.id,
                            "name":         tc.function.name,
                            "args":         args,
                            "domain_key":   domain_key,
                        }
                        status.update(label="Waiting for confirmation…", state="running", expanded=False)
                        st.rerun()

                    status.write(f"**Payload:** {json.dumps(args, ensure_ascii=False, indent=2)}")
                    result = execute_tool(tc.function.name, args, erp)

                    # Auto-attach proof file to newly created Payment Entry
                    if (tc.function.name == "create_payment_entry"
                            and result.get("success")
                            and st.session_state.get("proof_bytes")
                            and st.session_state.get("proof_filename")):
                        pe_name = result.get("data", {}).get("name")
                        if pe_name:
                            try:
                                erp.upload_file(
                                    st.session_state.proof_bytes,
                                    st.session_state.proof_filename,
                                    "Payment Entry", pe_name,
                                )
                                status.write(f"**Attachment:** proof uploaded to {pe_name}")
                            except Exception as att_err:
                                status.write(f"**Attachment warning:** {att_err}")

                    if result.get("_type") == "chart":
                        charts.append(result["chart"])
                        status.write(f"**Chart:** {result['chart'].get('title','')}")
                    elif result.get("_type") == "three_way":
                        st.session_state.three_way_result = result
                        produced_card = True
                        status.write(f"**Three-Way:** {result.get('status')}")
                    elif result.get("_type") in ("recon_report", "discrepancy"):
                        st.session_state.artifacts.append(result)
                        produced_card = True
                        status.write(f"**Artifact:** {result.get('message', result['_type'])}")
                    else:
                        status.write(f"**Result:** {json.dumps(result, ensure_ascii=False, indent=2)}")
                    conversation.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })

            if answer is None:
                answer = "Reached tool call limit — please rephrase your question."
                status.update(label="Done", state="complete", expanded=False)

        st.markdown(answer)
        render_doc_buttons(answer)
        save_message(st.session_state.session_id, domain_key, "assistant", answer)
        if total_time > 0 and total_tokens > 0:
            st.caption(f"⚡ {total_tokens / total_time:.1f} tok/s · {total_tokens} tokens")
        for chart in charts:
            _render_chart(chart)

    # Cards (three-way / report / discrepancy) live in the flow below the messages.
    # Rerun once so they render there this turn — unless a write confirmation is pending
    # (that path reruns on its own).
    if produced_card and not st.session_state.pending_write:
        st.rerun()

# ── Session state ─────────────────────────────────────────────────────────────

init_db()

for key, default in [
    ("stt_input", None), ("_stt_preview", None), ("_stt_edit_gen", 0),
    ("_stt_widget_gen", 0), ("_stt_last_seen", None),
    ("proof_data", None), ("proof_filename", None), ("proof_type", None), ("proof_bytes", None),
    ("conversations", {}), ("pending_write", None), ("resume_agent", False),
    ("three_way_result", None), ("artifacts", []),
    ("model_key", config.DEFAULT_MODEL),
    ("batch_queue", []), ("batch_total", 0), ("batch_pos", 0),
    ("batch_bank_month", ""), ("batch_sheet_url", ""), ("session_id", None),
    ("erp_cookies", None), ("erp_csrf", ""), ("erp_user", None), ("erp_roles", []),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# Assign a persistent session ID for this browser session
if not st.session_state.session_id:
    import uuid
    st.session_state.session_id = str(uuid.uuid4())[:8]

if st.session_state.get("_switch_to_staff"):
    st.session_state.selected_staff_radio = st.session_state.pop("_switch_to_staff")

# ── Login page (shown when not authenticated) ─────────────────────────────────

if not st.session_state.erp_user:
    st.markdown(
        "<style>[data-testid='stSidebar']{display:none}</style>",
        unsafe_allow_html=True,
    )
    _, col, _ = st.columns([1, 1, 1])
    with col:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.markdown("## 💱 Global Treasury Agent")
        st.markdown("Sign in with your ERPNext account to continue.")
        st.divider()
        with st.form("login_form"):
            _username = st.text_input("Username", placeholder="admin")
            _password = st.text_input("Password", type="password")
            _login_btn = st.form_submit_button("Sign In", type="primary", use_container_width=True)
        if _login_btn:
            if not _username or not _password:
                st.error("Please enter username and password.")
            else:
                try:
                    _cookies, _csrf = _auth.login(_username, _password)
                    _user = _auth.get_logged_user(_cookies)
                    _roles = _auth.get_user_roles(_user or _username)
                    st.session_state.erp_cookies = _cookies
                    st.session_state.erp_csrf    = _csrf
                    st.session_state.erp_user    = _user or _username
                    st.session_state.erp_roles   = _roles
                    st.rerun()
                except Exception as _e:
                    st.error(str(_e))
    st.stop()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("💱 Treasury Agent")
    st.divider()

    # ── Logged-in user + logout ───────────────────────────────────────────────
    st.success(f"✅ {st.session_state.erp_user}")
    if "System Manager" in st.session_state.erp_roles:
        st.caption("System Manager")
    else:
        _SKIP_ROLES = {"All", "Guest", "Desk User"}
        _display_roles = [r for r in st.session_state.erp_roles if r not in _SKIP_ROLES][:4]
        if _display_roles:
            st.caption(" · ".join(_display_roles))
    if st.button("Logout", use_container_width=True):
        st.session_state.erp_cookies = None
        st.session_state.erp_csrf    = ""
        st.session_state.erp_user    = None
        st.session_state.erp_roles   = []
        st.rerun()

    st.divider()

    selected_staff = list(STAFF.keys())[0]

    # ── New chat button ───────────────────────────────────────────────────────
    if st.button("➕ New Chat", use_container_width=True, type="primary"):
        import uuid
        st.session_state.session_id = str(uuid.uuid4())[:8]
        st.session_state.conversations = {}
        st.session_state.three_way_result = None
        st.rerun()

    # ── Session history list ──────────────────────────────────────────────────
    sessions = list_sessions()
    if sessions:
        st.markdown("**Recent Chats**")
        for s in sessions:
            is_active = s["session_id"] == st.session_state.session_id
            label = ("▶ " if is_active else "") + s["title"]
            col_btn, col_del = st.columns([5, 1])
            if col_btn.button(label, key=f"sess_{s['session_id']}",
                              use_container_width=True,
                              type="primary" if is_active else "secondary"):
                if not is_active:
                    st.session_state.session_id = s["session_id"]
                    st.session_state.conversations = {}
                    st.session_state.three_way_result = None
                    st.rerun()
            if col_del.button("🗑", key=f"del_{s['session_id']}"):
                delete_session(s["session_id"])
                if is_active:
                    import uuid
                    st.session_state.session_id = str(uuid.uuid4())[:8]
                    st.session_state.conversations = {}
                st.rerun()

    st.divider()
    st.markdown("**🧠 Model**")
    st.selectbox("Model", list(config.MODELS.keys()), key="model_key", label_visibility="collapsed")
    st.caption(f"`{config.MODELS[st.session_state.model_key]['model']}`")
    st.caption(f"ERP: `{config.ERPNEXT_URL}`")

    # ── Voice input ───────────────────────────────────────────────────────────
    st.divider()
    st.markdown("**🎤 Voice Input**")

    transcript = _speech_input(key=f"speech_input_widget_{st.session_state._stt_widget_gen}", default=None)

    if transcript and transcript != st.session_state.get("_stt_last_seen"):
        st.session_state._stt_last_seen = transcript
        existing = (st.session_state.get("_stt_preview") or "").strip()
        st.session_state._stt_preview = (existing + " " + transcript.strip()).strip() if existing else transcript
        st.session_state._stt_edit_gen += 1

    if st.session_state.get("_stt_preview") is not None:
        edit_key = f"stt_edit_{st.session_state._stt_edit_gen}"
        edited = st.text_area("Edit before sending", value=st.session_state._stt_preview, key=edit_key, height=80, label_visibility="collapsed")
        col_send, col_clear = st.columns(2)
        if col_send.button("Send ➤", type="primary", use_container_width=True):
            st.session_state.stt_input = edited
            st.session_state._stt_preview = None
            st.session_state._stt_last_seen = None
            st.rerun()
        if col_clear.button("Clear ✗", use_container_width=True):
            st.session_state._stt_preview = None
            st.session_state._stt_last_seen = None
            st.session_state._stt_widget_gen += 1
            st.rerun()

    # ── Bank statement from Google Sheets ────────────────────────────────────
    st.divider()
    st.markdown("**🏦 Bank Statement**")

    sheet_url = st.text_input(
        "Google Sheet URL",
        value=config.BANK_STATEMENT_SHEET_URL,
        placeholder="https://docs.google.com/spreadsheets/d/...",
        label_visibility="collapsed",
    )
    month = st.text_input(
        "Month tab",
        value="May2026",
        placeholder="e.g. May2026, Jan2026",
        label_visibility="collapsed",
    )
    st.caption("Used as the bank source when reconciling payment proofs.")

    # ── Payment proof batch upload ────────────────────────────────────────────
    st.divider()
    st.markdown("**📄 Upload Payment Proofs**")

    uploaded_docs = st.file_uploader(
        "Upload Payment Receipts",
        type=["pdf", "png", "jpg", "jpeg", "tiff", "bmp"],
        key="proof_uploader",
        label_visibility="collapsed",
        accept_multiple_files=True,
    )

    if uploaded_docs:
        st.caption(f"**{len(uploaded_docs)}** file(s) queued:")
        for d in uploaded_docs:
            st.caption(f"• {d.name}")

        if st.button(f"Start ({len(uploaded_docs)} document{'s' if len(uploaded_docs) > 1 else ''})",
                     use_container_width=True, type="primary"):
            # Snapshot bytes now — the uploader widget resets across reruns.
            st.session_state.batch_queue = [
                {"filename": d.name, "bytes": d.read()} for d in uploaded_docs
            ]
            st.session_state.batch_total     = len(st.session_state.batch_queue)
            st.session_state.batch_bank_month = month
            st.session_state.batch_sheet_url  = sheet_url or config.BANK_STATEMENT_SHEET_URL
            start_next_photo()   # kick off the first document
            st.rerun()


# ── Main area: payment proof review ──────────────────────────────────────────

if st.session_state.proof_data:
    render_payment_proof_review()
    st.stop()

# ── Chat area ─────────────────────────────────────────────────────────────────

domain_key = STAFF[selected_staff]
domain_cfg = get_domain_config(domain_key)
domain_tools = get_tools_for_domain(domain_cfg["read_tools"], domain_cfg["write_tools"])

if domain_key not in st.session_state.conversations:
    history = load_messages(st.session_state.session_id, domain_key)
    st.session_state.conversations[domain_key] = [
        {"role": "system", "content": domain_cfg["system_prompt"]},
        *history,
    ]

conversation = st.session_state.conversations[domain_key]

st.header(f"💬 {selected_staff}")

# Progress + manual "Next" control while a queue of documents is being processed.
if st.session_state.batch_total:
    remaining = len(st.session_state.batch_queue)
    done = st.session_state.batch_total - remaining
    st.progress(done / st.session_state.batch_total,
                text=f"Document {st.session_state.batch_pos} of {st.session_state.batch_total}"
                     + (f" · {remaining} remaining" if remaining else " · all done"))
    busy = st.session_state.resume_agent or st.session_state.pending_write
    if remaining and not busy:
        if st.button(f"➡️ Next document ({remaining} left)", type="primary"):
            start_next_photo()
            st.rerun()
    elif not remaining and not busy:
        if st.button("✓ Finish", help="Clear the document queue"):
            st.session_state.batch_total = 0
            st.session_state.batch_pos = 0
            st.rerun()


for msg in conversation:
    role = msg_role(msg)
    content = msg_content(msg)
    if role == "user":
        with st.chat_message("user"):
            img_url = msg_image_url(msg)
            if img_url:
                st.image(img_url, width=260)
            st.markdown(msg_text(msg))
    elif role == "assistant" and not has_tool_calls(msg) and msg_text(msg):
        with st.chat_message("assistant"):
            cleaned = strip_thinking(msg_text(msg))
            st.markdown(cleaned)
            render_doc_buttons(cleaned)

# Cards render in the conversation flow, right after the latest messages.
if st.session_state.three_way_result:
    with st.chat_message("assistant"):
        with st.expander("🔍 Three-Way Reconciliation", expanded=True):
            render_three_way_card(
                st.session_state.three_way_result,
                get_session_erp(),
                st.session_state.conversations.get("accounting", []),
            )

if st.session_state.artifacts:
    with st.chat_message("assistant"):
        st.markdown("**📎 Generated documents**")
        for i, art in enumerate(st.session_state.artifacts):
            render_artifact(art, i)
        if st.button("Clear documents", key="clear_artifacts"):
            st.session_state.artifacts = []
            st.rerun()

if st.session_state.pending_write:
    pending = st.session_state.pending_write
    if pending["domain_key"] == domain_key:
        erp = get_session_erp()
        conv = st.session_state.conversations[domain_key]
        with st.chat_message("assistant"):
            render_confirmation_card(pending, erp, conv)

stt_prompt = st.session_state.stt_input
if stt_prompt:
    st.session_state.stt_input = None

prompt = st.chat_input(f"Ask {selected_staff}…") or stt_prompt

if prompt and prompt.strip():
    prompt = prompt.strip()
    with st.chat_message("user"):
        st.markdown(prompt)
    conversation.append({"role": "user", "content": prompt})
    save_message(st.session_state.session_id, domain_key, "user", prompt)
    # Use first user message as session title
    user_msgs = [m for m in conversation if (m["role"] if isinstance(m, dict) else m.role) == "user"]
    if len(user_msgs) == 1:
        update_session_title(st.session_state.session_id, prompt)
    run_agent_loop(init_llm(st.session_state.model_key), get_session_erp(), conversation, domain_tools, domain_key)

elif st.session_state.resume_agent:
    st.session_state.resume_agent = False
    run_agent_loop(init_llm(st.session_state.model_key), get_session_erp(), conversation, domain_tools, domain_key)
