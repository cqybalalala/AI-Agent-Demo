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
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

import config
import auth as _auth
import guardrail
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
        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

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

# ── Global theme polish ───────────────────────────────────────────────────────
st.markdown(
    """
    <style>
      :root {
        --accent: #64748b;
        --accent-dark: #475569;
      }

      /* Comfortable reading width + breathing room */
      [data-testid="stMain"] .block-container {
        max-width: 1100px;
        padding-top: 2.2rem;
      }

      /* Headings: tighter, more confident */
      h1, h2, h3 { letter-spacing: -0.01em; font-weight: 700; }
      [data-testid="stHeader"] { background: transparent; }

      /* Buttons: rounded, smooth hover lift */
      .stButton > button {
        border-radius: 9px;
        font-weight: 600;
        transition: transform .06s ease, box-shadow .15s ease, border-color .15s ease;
      }
      .stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 3px 10px rgba(100,116,139,0.20);
        border-color: var(--accent);
      }
      .stButton > button[kind="primary"],
      [data-testid="stFormSubmitButton"] button {
        background: linear-gradient(135deg, var(--accent), var(--accent-dark));
        border: none;
        color: #fff;
      }
      [data-testid="stFormSubmitButton"] button:hover {
        transform: translateY(-1px);
        box-shadow: 0 3px 10px rgba(100,116,139,0.20);
      }

      /* Chat bubbles: soft card look */
      [data-testid="stChatMessage"] {
        border-radius: 14px;
        padding: 0.4rem 0.6rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      }

      /* Chat input + text inputs: rounded with accent focus */
      [data-testid="stChatInput"] { border-radius: 12px; }
      [data-testid="stChatInput"]:focus-within {
        border-color: var(--accent);
        box-shadow: 0 0 0 2px rgba(100,116,139,0.18);
      }
      .stTextInput input:focus, .stTextArea textarea:focus {
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 2px rgba(100,116,139,0.18) !important;
      }

      /* Sidebar: rounded full-width buttons, lighter dividers */
      [data-testid="stSidebar"] .stButton > button { border-radius: 8px; }
      hr { opacity: 0.5; }
    </style>
    """,
    unsafe_allow_html=True,
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

def _render_image_grid(items: list[tuple[bytes, str]], width: int = 120):
    """Lay out (image_bytes, caption) pairs as small fixed-width thumbnails."""
    if not items:
        return
    cols = st.columns(min(len(items), 4))
    for idx, (img, cap) in enumerate(items):
        with cols[idx % len(cols)]:
            st.image(img, caption=cap, width=width)

def render_upload_thumbs(files):
    """Thumbnails for freshly-uploaded files (Streamlit UploadedFile objects)."""
    items = []
    for f in files:
        if Path(f.name).suffix.lower() == ".pdf":
            continue
        try:
            items.append((f.getvalue(), f.name))
        except Exception:
            pass
    _render_image_grid(items)

def render_proof_thumbs(rows):
    """Thumbnails for batch rows (image files only; PDFs have no inline preview)."""
    items = [(r["bytes"], r["filename"]) for r in rows
             if r.get("bytes") and Path(r["filename"]).suffix.lower() != ".pdf"]
    _render_image_grid(items)

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
        add_trace("✋ USER", f"approved `{name}`")
        # reference_no on a Payment Entry must be the BANK transaction id, NOT an invoice number.
        # The model sometimes fills it with the invoice name — override it with the matched bank
        # entry's reference from the three-way result (deterministic, doesn't trust the model).
        if name == "create_payment_entry":
            bank_ref = ((st.session_state.get("three_way_result") or {}).get("bank") or {}).get("reference")
            if bank_ref and args.get("reference_no") != bank_ref:
                add_trace("⚙️ APP", f"fixed reference_no: {args.get('reference_no')} → {bank_ref} (bank txn id)")
                args["reference_no"] = bank_ref
        result = execute_tool(name, args, erp)
        add_trace("⚙️ APP", f"executed `{name}` → "
                            f"{'ok' if result.get('success') else 'FAIL'}"
                            + (f" · PE={result.get('data',{}).get('name')}"
                               f" forex={result.get('forex')} bank_charge={result.get('bank_charge')}"
                               if name == 'create_payment_entry' and result.get('success') else ""))
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
                # Pass the PROJECTED forex figures from the create result — the PE is still a
                # draft here, so its GL / linked Exchange-Gain/Loss JV don't exist yet. Both the
                # PE-side spread (pe_forex) and the invoice-vs-payment movement (jv_forex) are
                # deterministic and will match the ledger once the user submits in ERPNext.
                tw = st.session_state.get("three_way_result") or {}
                rep = execute_tool("generate_reconciliation_report", {
                    "payment_entry": pe_name,
                    "confidence":    tw.get("confidence"),
                    "match_method":  tw.get("match_method"),
                    "forex":         result.get("forex"),
                    "pe_forex":      result.get("pe_forex"),
                    "jv_forex":      result.get("jv_forex"),
                    "bank_charge":   result.get("bank_charge"),
                }, erp)
                if rep.get("_type"):
                    # Mirror the agent-loop path: give the artifact an _id and anchor it with
                    # a marker message, otherwise it lands in session state but never renders.
                    rep["_id"] = __import__("uuid").uuid4().hex[:8]
                    st.session_state.artifacts.append(rep)
                    dk = pending.get("domain_key", "accounting")
                    title = f"📄 Reconciliation Report — {rep.get('data', {}).get('payment_entry','')}"
                    conversation.append({"role": "assistant", "content": title,
                                         "_artifact_id": rep["_id"]})
                    save_message(st.session_state.session_id, dk, "assistant", title)
                    add_trace("⚙️ APP", f"auto-generated report → expected_myr="
                                        f"{rep.get('data',{}).get('expected_myr')}")

                # Corrected-reference detection — done HERE at the approval gate, deterministically.
                # Does NOT rely on the model re-running three_way: we compare the invoice the proof
                # ORIGINALLY cited (from the extracted reference_no) against the invoice we're
                # actually posting to. If they differ, a human steered the posting → record it.
                import re as _re
                m = _re.search(r"[A-Z]{2,5}-[A-Z]{2,5}-\d{4}-\d{5}",
                               str(pending.get("proof_orig_ref") or "").upper())
                cited  = m.group(0) if m else ""
                posted = str(args.get("invoice_name") or "").upper()
                if cited and posted and cited != posted:
                    fx = result.get("forex")
                    note = (f"Documented correction: payment proof cited {cited}, but posted against "
                            f"operator-confirmed {args.get('invoice_name')}. "
                            f"Booked amount MYR {args.get('bank_amount')}.")
                    if result.get("bank_charge") is not None:
                        note += f" Bank charge MYR {result.get('bank_charge')}."
                    if fx is not None:
                        note += f" Forex {'loss' if fx > 0 else 'gain'} MYR {abs(fx)}."
                    try:
                        erp.add_comment("Payment Entry", pe_name, note)
                        add_trace("⚙️ APP", f"detected reference correction ({cited} → "
                                            f"{args.get('invoice_name')}) → added audit comment")
                    except Exception as e:
                        add_trace("⚙️ APP", f"audit comment FAILED: {e}")
        conversation.append({
            "role": "tool", "tool_call_id": tc_id,
            "content": json.dumps(result, ensure_ascii=False),
        })
        st.session_state.pending_write = None
        st.session_state.resume_agent = True
        st.rerun()

    if cancelled:
        add_trace("✋ USER", f"cancelled `{name}`")
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
    "PARTIAL":           ("⚠️", "warning",  "Needs review before posting"),
    "PENDING":           ("🕐", "info",     "Invoice matched but payment not yet in bank"),
    "UNMATCHED_INVOICE": ("❓", "warning",  "Bank entry found but no matching invoice"),
    "UNMATCHED":         ("❌", "error",    "No matching invoice or bank entry found"),
    "ALREADY_PAID":      ("💚", "success",  "Invoice already fully paid — no action needed"),
}

def render_three_way_card(result: dict, erp, conversation: list):
    status = result.get("status", "UNMATCHED")
    icon, stype, note = STATUS_META.get(status, ("❓", "info", ""))

    st.subheader(f"{icon} {status} — {note}")
    if result.get("note"):
        st.warning(result["note"])

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
        # Read-only summary only. The actual Payment Entry is created via the agent's
        # create_payment_entry tool, which surfaces the 💳 confirmation card below
        # (that path also attaches the proof and generates the reconciliation report).
        st.markdown(f"**Ready to post:** Payment Entry for **{pe.get('party')}** — "
                    f"Received MYR {pe.get('received_amount')}")

# ── Batch payment-proof reconciliation (conversational, all at once) ──────────

BATCH_STATUS_ICON = {
    "RECONCILED":        "✅",
    "ALREADY_PAID":      "💚",
    "PARTIAL":           "⚠️",
    "PENDING":           "🕐",
    "UNMATCHED_INVOICE": "❓",
    "UNMATCHED":         "❌",
    "ERROR":             "💥",
}

def _counterparty_name(data: dict) -> str:
    return (data.get("sender_name") or data.get("receiver_name") or "").strip()

def process_proof_batch(files, bank_month: str, sheet_url: str, erp, on_event=None) -> list[dict]:
    """Extract + three-way-reconcile each uploaded proof. One row dict per file.

    Reconciliation runs deterministically here (no agent loop) so a whole batch
    can be processed in one pass and summarised in a table. Nothing is created —
    Payment Entries are only created later, after the user confirms.

    `on_event(done, total, message)` is called as each document moves through the
    pipeline, so the UI can narrate progress live instead of blocking on a spinner.
    """
    def emit(done, msg):
        if on_event:
            on_event(done, len(files), msg)

    rows = []
    for i, f in enumerate(files):
        name = f.name
        emit(i, f"📄 Reading **{name}** ({i + 1} of {len(files)})…")
        try:
            file_bytes = f.getvalue()
        except Exception:
            file_bytes = f.read()
        row = {"filename": name, "bytes": file_bytes, "created": False,
               "created_pe": None, "error": None, "data": {}, "reconcile": None,
               "status": "ERROR", "can_create": False}

        try:
            data = extract_payment_receipt(file_bytes, name)
        except Exception as e:
            row["error"] = f"couldn't read document: {e}"
            emit(i + 1, f"💥 **{name}** — couldn't read it: {e}")
            rows.append(row)
            continue
        row["data"] = data

        who = _counterparty_name(data) or "unknown party"
        amt = f'{(data.get("currency") or "").strip()} {data.get("amount_paid") or "?"}'.strip()
        emit(i, f"🔎 **{name}** — {who}, {amt}. Matching invoice & bank statement…")

        args = {
            "amount":       float(data.get("amount_paid") or 0),
            "currency":     (data.get("currency") or "MYR").upper(),
            "customer":     _counterparty_name(data),
            "reference":    data.get("reference_no") or "",
            "payment_date": data.get("payment_date"),
            "bank_month":   bank_month,
            "sheet_url":    sheet_url,
        }
        try:
            rec = execute_tool("three_way_reconcile", args, erp)
        except Exception as e:
            row["error"] = f"reconciliation failed: {e}"
            emit(i + 1, f"💥 **{name}** — reconciliation failed: {e}")
            rows.append(row)
            continue

        row["reconcile"]  = rec
        row["status"]     = rec.get("status", "UNMATCHED")
        row["can_create"] = bool(rec.get("ready_for_payment_entry"))
        rows.append(row)

        icon  = BATCH_STATUS_ICON.get(row["status"], "")
        if row["can_create"]:
            inv = (rec.get("invoice") or {}).get("name", "")
            emit(i + 1, f"{icon} **{name}** → {row['status']} (matched {inv}, "
                        f"conf {rec.get('confidence', '—')}). Ready to create.")
        else:
            emit(i + 1, f"{icon} **{name}** → {row['status']} — needs review, won't auto-create.")
    return rows

def _create_selected_proofs(indices: list[int], erp, batch: dict):
    """Create a Payment Entry for each selected (and creatable) batch row."""
    rows = batch["rows"]
    created, failed = [], []
    for i in indices:
        r = rows[i]
        if not r.get("can_create") or r.get("created"):
            continue
        rec  = r.get("reconcile") or {}
        inv  = rec.get("invoice") or {}
        bank = rec.get("bank") or {}
        data = r.get("data") or {}
        args = {
            "invoice_name": inv.get("name"),
            "invoice_type": inv.get("doctype"),
            "bank_amount":  bank.get("amount") or (rec.get("fx") or {}).get("converted"),
            "payment_date": data.get("payment_date"),
            "reference_no": bank.get("reference") or data.get("reference_no") or "",
        }
        try:
            res = execute_tool("create_payment_entry", args, erp)
        except Exception as e:
            r["error"] = f"create failed: {e}"
            failed.append(r)
            continue
        if res.get("success"):
            pe_name = (res.get("data") or {}).get("name")
            r["created"]     = True
            r["created_pe"]  = pe_name
            r["forex"]       = res.get("forex")
            r["bank_charge"] = res.get("bank_charge")
            created.append(r)
            # Attach the original proof file to the new Payment Entry.
            if pe_name and r.get("bytes"):
                try:
                    erp.upload_file(r["bytes"], r["filename"], "Payment Entry", pe_name)
                except Exception:
                    pass
        else:
            r["error"] = res.get("error", "create failed")
            failed.append(r)

    # Record this round's outcome to the conversation + DB so it persists and
    # the agent knows which Payment Entries now exist.
    if created or failed:
        parts = []
        if created:
            parts.append("✅ Created Payment Entries:\n" + "\n".join(
                f"- **{r['created_pe']}** — {r['filename']} "
                f"({_counterparty_name(r.get('data') or {}) or '—'})"
                for r in created))
        if failed:
            parts.append("❌ Failed:\n" + "\n".join(
                f"- {r['filename']}: {r.get('error')}" for r in failed))
        note = "\n\n".join(parts)
        conv = st.session_state.conversations.setdefault(
            "accounting",
            [{"role": "system", "content": get_domain_config("accounting")["system_prompt"]}])
        conv.append({"role": "assistant", "content": note})
        save_message(st.session_state.session_id, "accounting", "assistant", note)

    # Bump the batch version so the editor + cached PDF refresh with the new state.
    batch["ver"] += 1

def _proof_issue(r: dict) -> str:
    """One-line human explanation of what's wrong / next step for a proof."""
    if r.get("error"):
        return r["error"]
    rec    = r.get("reconcile") or {}
    if rec.get("note"):                       # e.g. reference-vs-match contradiction
        return rec["note"]
    status = r.get("status")
    note   = STATUS_META.get(status, ("", "", "Needs manual review."))[2]
    if status == "PARTIAL" and rec.get("diff_pct") is not None:
        note += f" (bank vs proof differ {rec['diff_pct']:+.2f}%)"
    return note

def _batch_summary_markdown(rows: list[dict]) -> str:
    """Persistent, LLM-readable reconciliation report for a processed batch."""
    n_ready = sum(1 for r in rows if r.get("can_create"))
    lines = [f"Processed **{len(rows)}** payment proof(s) — {n_ready} ready to create. "
             f"Reconciliation results:\n"]
    for r in rows:
        rec   = r.get("reconcile") or {}
        inv   = rec.get("invoice") or {}
        bank  = rec.get("bank") or {}
        data  = r.get("data") or {}
        icon  = BATCH_STATUS_ICON.get(r["status"], "")
        party = _counterparty_name(data) or "—"
        amt   = f'{(data.get("currency") or "").strip()} {data.get("amount_paid") or "?"}'.strip()
        lines.append(f"\n**{icon} {r['filename']} — {r['status']}**")
        lines.append(f"- Counterparty: {party} · Amount: {amt} · "
                     f"Ref: {data.get('reference_no') or '—'} · Date: {data.get('payment_date') or '—'}")
        if not r.get("error"):
            lines.append(f"- Invoice: {inv.get('name')} ({inv.get('doctype')}), "
                         f"outstanding {inv.get('currency','')} {inv.get('amount','')}"
                         if inv else "- Invoice: no matching outstanding invoice found")
            lines.append(f"- Bank: MYR {bank.get('amount')} on {bank.get('date','—')} "
                         f"(ref {bank.get('reference','—')})"
                         if bank else "- Bank: not found in the bank statement")
            lines.append(f"- Confidence: {rec.get('confidence','—')} · "
                         f"Match: {rec.get('match_method') or '—'}")
        lines.append(f"- **Issue:** {_proof_issue(r)}")
    lines.append("\nTick the ✅ RECONCILED rows in the table above and click "
                 "**Create selected** to post their Payment Entries.")
    return "\n".join(lines)

def _batch_intro_md(rows: list[dict], n_ready: int) -> str:
    """Short conversational lead-in so the agent 'talks' above the table."""
    n = len(rows)
    lines = [f"I went through {'your ' if n == 1 else ''}{n} payment proof"
             f"{'' if n == 1 else 's'} — here's what I found:"]
    for r in rows:
        data  = r.get("data") or {}
        icon  = BATCH_STATUS_ICON.get(r["status"], "")
        party = _counterparty_name(data) or "this proof"
        amt   = f'{(data.get("currency") or "").strip()} {data.get("amount_paid") or "?"}'.strip()
        lines.append(f"- {icon} **{r['filename']}** — {party} ({amt}): {_proof_issue(r)}")
    if n_ready:
        lines.append(f"\n**{n_ready}** {'is' if n_ready == 1 else 'are'} reconciled and ready to post — "
                     f"tick {'it' if n_ready == 1 else 'them'} in the table below and click **Create selected**.")
    else:
        lines.append("\nNone are auto-creatable yet — open the table below for the details on each.")
    return "\n".join(lines)

def render_batch_table(batch: dict, erp, is_active: bool):
    """Render one batch as a card anchored in the chat flow.

    The latest batch (is_active) is expanded and its Create button works. Older
    batches collapse to a read-only summary once the conversation has moved on.
    """
    rows = batch["rows"]
    bid, ver = batch["id"], batch["ver"]
    n_ready = sum(1 for r in rows if r.get("can_create") and not r.get("created"))
    n_done  = sum(1 for r in rows if r.get("created"))

    with st.chat_message("assistant"):
        # The agent's spoken summary, shown above the table so it isn't a bare
        # data dump. Only on the active batch; older ones stay collapsed.
        if is_active:
            st.markdown(_batch_intro_md(rows, n_ready))
        header = (f"📊 {len(rows)} payment proof(s) · {n_ready} ready to create"
                  + (f" · {n_done} created" if n_done else "")
                  + ("" if is_active else " · (read-only)"))
        with st.expander(header, expanded=is_active):

            # Consolidated PDF report (matched + discrepancies), cached per
            # batch version so toggling checkboxes doesn't regenerate it.
            pdf_key = f"_batch_pdf_{bid}_{ver}"
            if pdf_key not in st.session_state:
                try:
                    st.session_state[pdf_key] = _html_to_pdf(_batch_report_html(rows))
                except Exception as e:
                    st.session_state[pdf_key] = None
                    st.caption(f"(report PDF unavailable: {e})")
            if st.session_state.get(pdf_key):
                st.download_button(
                    "📄 Download full reconciliation report",
                    st.session_state[pdf_key],
                    file_name="batch_reconciliation_report.pdf",
                    mime="application/pdf",
                    key=f"dl_batch_{bid}_{ver}",
                    use_container_width=True,
                )

            table_rows = []
            for r in rows:
                rec  = r.get("reconcile") or {}
                inv  = rec.get("invoice") or {}
                bank = rec.get("bank") or {}
                data = r.get("data") or {}
                table_rows.append({
                    "✓":            bool(is_active and r.get("can_create") and not r.get("created")),
                    "Status":       f'{BATCH_STATUS_ICON.get(r["status"], "")} {r["status"]}',
                    "File":         r["filename"],
                    "Counterparty": _counterparty_name(data) or "—",
                    "Amount":       f'{data.get("currency") or ""} {data.get("amount_paid") or "—"}'.strip(),
                    "Invoice":      inv.get("name") or "—",
                    "Bank (MYR)":   bank.get("amount") if bank else None,
                    "Conf.":        rec.get("confidence") if rec else None,
                    "Created":      r.get("created_pe") or ("✓" if r.get("created") else "—"),
                })

            df = pd.DataFrame(table_rows)
            # Inactive batches are fully read-only; active ones allow ticking ✓.
            disabled = True if not is_active else [c for c in df.columns if c != "✓"]
            edited = st.data_editor(
                df,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "✓": st.column_config.CheckboxColumn(
                        "✓", help="Create a Payment Entry for this row", width="small"),
                },
                disabled=disabled,
                key=f"batch_editor_{bid}_{ver}",
            )

            if is_active:
                st.caption("Only ✅ RECONCILED rows can be created. Rows that need "
                           "review are listed below — they can't be created automatically.")
                selected = [i for i, r in enumerate(rows)
                            if bool(edited.iloc[i]["✓"]) and r.get("can_create")
                            and not r.get("created")]
                col_create, col_clear = st.columns([3, 1])
                if col_create.button(f"💳 Create selected ({len(selected)})", type="primary",
                                     disabled=not selected, use_container_width=True,
                                     key=f"create_{bid}_{ver}"):
                    _create_selected_proofs(selected, erp, batch)
                    st.rerun()
                if col_clear.button("Dismiss", use_container_width=True, key=f"clr_{bid}"):
                    st.session_state.batches = [b for b in st.session_state.batches
                                                if b["id"] != bid]
                    st.rerun()
            else:
                st.caption("This batch is read-only — a newer turn has started. "
                           "Upload again to reconcile new proofs.")

            # Detail cards for rows that aren't auto-creatable.
            for r in rows:
                if r.get("reconcile") and not r.get("can_create") and not r.get("created"):
                    with st.expander(f'{BATCH_STATUS_ICON.get(r["status"], "")} '
                                     f'{r["filename"]} — {r["status"]}'):
                        render_three_way_card(
                            r["reconcile"], erp,
                            st.session_state.conversations.get("accounting", []))

            for r in rows:
                if r.get("error"):
                    st.error(f'`{r["filename"]}` — {r["error"]}')

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
    axlabel = f"{ylabel} ({currency})" if currency else ylabel
    multi = len(datasets) > 1

    if ctype in ("pie", "donut"):
        fig = px.pie(names=labels, values=values, title=title, hole=0.4 if ctype == "donut" else 0)
    elif multi:
        # Several series on the same axis (e.g. PE forex vs cross-table JV forex). Draw one
        # trace per dataset; stack them when the chart asks for it, otherwise group side by side.
        horizontal = ctype == "horizontal-bar"
        fig = go.Figure()
        for ds in datasets:
            vals, lbl, col = ds.get("values", []), ds.get("label", ""), ds.get("color")
            if ctype == "line":
                fig.add_trace(go.Scatter(x=labels, y=vals, mode="lines+markers",
                                         name=lbl, line={"color": col} if col else None))
            elif horizontal:
                fig.add_trace(go.Bar(y=labels, x=vals, orientation="h",
                                     name=lbl, marker_color=col))
            else:
                fig.add_trace(go.Bar(x=labels, y=vals, name=lbl, marker_color=col))
        if ctype != "line":
            fig.update_layout(barmode="stack" if chart.get("stacked") else "group")
        axis = "xaxis_title" if horizontal else "yaxis_title"
        fig.update_layout(title=title, **{axis: currency or ""})
    elif ctype == "line":
        fig = px.line(x=labels, y=values, title=title, labels={"x": "", "y": axlabel}, markers=True)
        fig.update_traces(line_color=color)
    elif ctype == "horizontal-bar":
        fig = px.bar(x=values, y=labels, orientation="h", title=title, labels={"x": axlabel, "y": ""})
        fig.update_traces(marker_color=color)
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
    else:
        fig = px.bar(x=labels, y=values, title=title, labels={"x": "", "y": axlabel})
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

def _fx_label(forex) -> str:
    """Render the forex deduction: +ve = loss, -ve = gain (ERPNext sign convention)."""
    v = float(forex or 0)
    if v > 0:
        return f"MYR {v:.2f} loss"
    if v < 0:
        return f"MYR {abs(v):.2f} gain"
    return "MYR 0.00 (none)"


def _recon_report_html(d: dict) -> str:
    fx = ""
    if d.get("paid_currency") and d.get("paid_currency") != "MYR":
        fx = f"{d.get('paid_currency')} → MYR @ {d.get('exchange_rate')}"
    # Forex: split into the invoice-vs-payment rate movement (the linked JV) and the settlement
    # spread (the PE's own exchange line) when we have both; otherwise show a single total.
    proj = " — projected, posts on submit" if d.get("forex_projected") else ""
    if d.get("pe_forex") is not None and d.get("jv_forex") is not None:
        forex_rows = [
            ("Forex · invoice vs payment rate (MYR)", _fx_label(d.get("jv_forex"))),
            ("Forex · settlement spread (MYR)", _fx_label(d.get("pe_forex"))),
            (f"Forex total (MYR){proj}", _fx_label(d.get("forex"))),
        ]
    elif d.get("forex") is not None:
        forex_rows = [(f"Forex gain/loss (MYR){proj}", _fx_label(d.get("forex")))]
    else:
        forex_rows = []
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
        ("Bank charge (MYR)", f"MYR {d.get('bank_charge')}" if d.get("bank_charge") is not None else None),
        *forex_rows,
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

_BATCH_BADGE = {
    "RECONCILED":        ("#bbf7d0", "#14532d"),
    "ALREADY_PAID":      ("#bbf7d0", "#14532d"),
    "PARTIAL":           ("#fef08a", "#713f12"),
    "PENDING":           ("#bfdbfe", "#1e3a8a"),
    "UNMATCHED_INVOICE": ("#fed7aa", "#7c2d12"),
    "UNMATCHED":         ("#fecaca", "#7f1d1d"),
    "ERROR":             ("#fecaca", "#7f1d1d"),
}

def _batch_report_html(rows: list[dict]) -> str:
    """One consolidated PDF report covering a whole batch — matched AND unmatched."""
    from datetime import date
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    n_ready   = sum(1 for r in rows if r.get("can_create"))
    n_created = sum(1 for r in rows if r.get("created"))
    chips = " ".join(
        f'<span class="badge" style="background:#e2e8f0;color:#1e293b;">'
        f'{s}: {c}</span>' for s, c in counts.items())

    sections = []
    for i, r in enumerate(rows, 1):
        rec  = r.get("reconcile") or {}
        inv  = rec.get("invoice") or {}
        bank = rec.get("bank") or {}
        data = r.get("data") or {}
        bg, fg = _BATCH_BADGE.get(r["status"], ("#e2e8f0", "#1e293b"))
        body = _rows_html([
            ("Counterparty",        _counterparty_name(data)),
            ("Amount",              f'{data.get("currency","")} {data.get("amount_paid","")}'),
            ("Reference",           data.get("reference_no")),
            ("Payment Date",        data.get("payment_date")),
            ("Matched Invoice",     f'{inv.get("name")} ({inv.get("doctype")})' if inv else None),
            ("Invoice Outstanding", f'{inv.get("currency","")} {inv.get("amount","")}' if inv else None),
            ("Bank (MYR)",          f'{bank.get("amount")} on {bank.get("date","")}' if bank else "not found"),
            ("Confidence",          rec.get("confidence")),
            ("Match Method",        rec.get("match_method")),
            ("Payment Entry",       r.get("created_pe")),
            ("Bank charge (MYR)",   f'MYR {r.get("bank_charge")}' if r.get("bank_charge") is not None else None),
            ("Forex gain/loss (MYR)", _fx_label(r.get("forex")) if r.get("forex") is not None else None),
        ])
        sections.append(
            f'<h2 style="font-size:12pt;margin-top:16pt;">{i}. {r["filename"]} '
            f'<span class="badge" style="background:{bg};color:{fg};">{r["status"]}</span></h2>'
            f'<table>{body}</table>'
            f'<p style="font-size:9.5pt;"><b>Issue / next step:</b> {_proof_issue(r)}</p>'
        )

    return f"""<html><head>{_PDF_CSS}</head><body>
        <h1>Batch Reconciliation Report</h1>
        <div class="sub">{len(rows)} payment proof(s) &middot; {n_ready} ready to create &middot;
            {n_created} created &middot; {date.today()}</div>
        <p>{chips}</p>
        {''.join(sections)}
        <div class="foot">Generated by Treasury Agent &middot; figures sourced live from ERPNext.</div>
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

def add_trace(source: str, text: str):
    """Append one line to the persistent debug tool-call trace (survives reruns).
    `source` marks WHO acted: '🤖 MODEL' vs '⚙️ APP'. Used to diagnose who calls what."""
    import datetime
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    st.session_state.tool_trace.append(f"`{ts}` {source} {text}")


def run_agent_loop(llm, erp, conversation, domain_tools, domain_key):
    with st.chat_message("assistant"):
        answer = None
        narration = []          # every non-empty assistant text across the loop
        charts = []
        produced_artifacts = []
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
                    narration.append(llm_text)

                if not msg.tool_calls:
                    # Fall back to earlier narration if the final message is empty.
                    answer = llm_text or "\n\n".join(narration)
                    status.update(label="Done", state="complete", expanded=False)
                    break

                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    status.write(f"**Tool:** `{tc.function.name}`")

                    if tc.function.name in WRITE_TOOLS:
                        add_trace("🤖 MODEL", f"requested WRITE `{tc.function.name}` → ⏸ awaiting your approval "
                                              f"· args={json.dumps(args, ensure_ascii=False)}")
                        st.session_state.pending_write = {
                            "tool_call_id": tc.id,
                            "name":         tc.function.name,
                            "args":         args,
                            "domain_key":   domain_key,
                            # Snapshot the proof's original cited reference AT CALL TIME, bound to
                            # THIS approval — so override detection can't use a stale global value.
                            "proof_orig_ref": st.session_state.get("proof_orig_ref"),
                        }
                        status.update(label="Waiting for confirmation…", state="running", expanded=False)
                        st.rerun()

                    status.write(f"**Payload:** {json.dumps(args, ensure_ascii=False, indent=2)}")
                    result = execute_tool(tc.function.name, args, erp)
                    add_trace("🤖 MODEL", f"called `{tc.function.name}` → "
                                          f"{'ok' if result.get('success', True) else 'FAIL'}"
                                          + (f" · forex={result.get('forex')} bank_charge={result.get('bank_charge')}"
                                             if result.get('forex') is not None else ""))

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
                        result["_id"] = __import__("uuid").uuid4().hex[:8]
                        st.session_state.three_way_cards[result["_id"]] = result
                        produced_card = True
                        status.write(f"**Three-Way:** {result.get('status')}")
                    elif result.get("_type") in ("recon_report", "discrepancy"):
                        result["_id"] = __import__("uuid").uuid4().hex[:8]
                        st.session_state.artifacts.append(result)
                        produced_artifacts.append(result)
                        produced_card = True
                        status.write(f"**Artifact:** {result.get('message', result['_type'])}")
                    else:
                        status.write(f"**Result:** {json.dumps(result, ensure_ascii=False, indent=2)}")
                    conversation.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                    # Anchor the three-way card in the flow at this point (a marker
                    # message), so it stays put instead of pinning to the bottom.
                    if result.get("_type") == "three_way":
                        marker = "🔍 Three-Way Reconciliation"
                        conversation.append({"role": "assistant", "content": marker,
                                             "_three_way_id": result["_id"]})
                        save_message(st.session_state.session_id, domain_key, "assistant", marker)

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

    # Anchor each generated document as a card in the chat flow (not pinned to the
    # bottom). A marker message ties the artifact to this point in the history.
    for art in produced_artifacts:
        data = art.get("data", {})
        title = (f"📄 Reconciliation Report — {data.get('payment_entry','')}"
                 if art.get("_type") == "recon_report"
                 else f"⚠️ Discrepancy Summary — {data.get('payer','')}")
        conversation.append({"role": "assistant", "content": title, "_artifact_id": art["_id"]})
        save_message(st.session_state.session_id, domain_key, "assistant", title)

    # Cards (three-way / report / discrepancy) live in the flow below the messages.
    # Rerun once so they render there this turn — unless a write confirmation is pending
    # (that path reruns on its own).
    if produced_card and not st.session_state.pending_write:
        st.rerun()

# ── Session state ─────────────────────────────────────────────────────────────

init_db()

for key, default in [
    ("stt_input", None), ("_stt_widget_gen", 0),
    ("proof_data", None), ("proof_filename", None), ("proof_type", None), ("proof_bytes", None),
    ("conversations", {}), ("pending_write", None), ("resume_agent", False),
    ("three_way_result", None), ("three_way_cards", {}), ("artifacts", []),
    ("model_key", config.DEFAULT_MODEL),
    ("batches", []), ("active_batch_id", None), ("session_id", None),
    ("erp_cookies", None), ("erp_csrf", ""), ("erp_user", None), ("erp_roles", []),
    ("erp_user_image", ""),
    ("tool_trace", []),   # debug: persistent who-called-what log (survives reruns)
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
    # Make the login card noticeably bigger and taller-feeling
    st.markdown(
        """
        <style>
          /* widen + enlarge the login form fields and button */
          [data-testid="stForm"] { padding: 0.5rem 0.25rem; }
          [data-testid="stForm"] input {
            height: 3rem; font-size: 1.05rem; padding: 0.6rem 0.9rem;
          }
          [data-testid="stForm"] button { height: 3rem; font-size: 1.05rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.markdown("# 💱 Global Treasury Agent")
        st.markdown("#### Sign in with your ERPNext account to continue.")
        with st.container(border=True):
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
                    _doc = _auth.get_user_doc(_user or _username)
                    _roles = [row["role"] for row in _doc.get("roles", []) if row.get("role")]
                    st.session_state.erp_cookies = _cookies
                    st.session_state.erp_csrf    = _csrf
                    st.session_state.erp_user    = _user or _username
                    st.session_state.erp_roles   = _roles
                    st.session_state.erp_user_image = _auth.image_url_from_doc(_doc)
                    st.rerun()
                except Exception as _e:
                    st.error(str(_e))
    st.stop()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("💱 Treasury Agent")
    st.divider()

    selected_staff = list(STAFF.keys())[0]

    # ── New chat button ───────────────────────────────────────────────────────
    if st.button("➕ New Chat", use_container_width=True, type="primary"):
        import uuid
        st.session_state.session_id = str(uuid.uuid4())[:8]
        st.session_state.conversations = {}
        st.session_state.three_way_result = None
        st.session_state.three_way_cards = {}
        st.session_state.artifacts = []
        st.session_state.batches = []
        st.session_state.active_batch_id = None
        st.session_state.tool_trace = []
        st.rerun()

    # ── Tool-call trace (debug: who called what) ──────────────────────────────
    with st.expander(f"🔧 Tool trace ({len(st.session_state.tool_trace)})",
                     expanded=bool(st.session_state.tool_trace)):
        if st.session_state.tool_trace:
            st.markdown("\n".join(f"- {line}" for line in st.session_state.tool_trace))
            if st.button("Clear trace", use_container_width=True):
                st.session_state.tool_trace = []
                st.rerun()
        else:
            st.caption("🤖 MODEL vs ⚙️ APP actions show here as you reconcile.")

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
                    st.session_state.three_way_cards = {}
                    st.session_state.artifacts = []
                    st.session_state.batches = []
                    st.session_state.active_batch_id = None
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
    st.caption("Used as the bank source when reconciling payment proofs. "
               "Attach proofs directly in the chat box to reconcile them.")

    # ── Logged-in user + logout (pinned to bottom) ────────────────────────────
    st.divider()
    _user_email = st.session_state.erp_user or ""
    if "System Manager" in st.session_state.erp_roles:
        _role_text = "System Manager"
    else:
        _SKIP_ROLES = {"All", "Guest", "Desk User"}
        _display_roles = [r for r in st.session_state.erp_roles if r not in _SKIP_ROLES][:4]
        _role_text = " · ".join(_display_roles) if _display_roles else ""

    _initial = (_user_email.strip()[:1] or "?").upper()
    _img = st.session_state.get("erp_user_image") or ""
    if _img:
        _avatar = (
            f'<img src="{_img}" alt="avatar" '
            'style="flex:0 0 auto;width:38px;height:38px;border-radius:50%;'
            'object-fit:cover;" />'
        )
    else:
        _avatar = (
            '<div style="flex:0 0 auto;width:38px;height:38px;border-radius:50%;'
            'background:linear-gradient(135deg,#94a3b8,#475569);'
            'color:#fff;font-weight:700;font-size:16px;'
            'display:flex;align-items:center;justify-content:center;">'
            f'{_initial}</div>'
        )
    col_prof, col_logout = st.columns([5, 1], vertical_alignment="center")
    with col_prof:
        st.markdown(
            f"""
            <div style="display:flex;align-items:center;gap:10px;padding:4px 0;">
              {_avatar}
              <div style="min-width:0;line-height:1.25;">
                <div style="font-weight:600;font-size:13px;color:inherit;
                            white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                  {_user_email}
                </div>
                <div style="font-size:11px;opacity:0.65;">{_role_text}</div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col_logout:
        if st.button("🚪", help="Logout", key="logout_btn", use_container_width=True):
            st.session_state.erp_cookies = None
            st.session_state.erp_csrf    = ""
            st.session_state.erp_user    = None
            st.session_state.erp_roles   = []
            st.session_state.erp_user_image = ""
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
st.caption("Ask about receivables, payables, payments & reconciliation — grounded in live ERPNext data.")

for msg in conversation:
    role = msg_role(msg)
    # Hidden context (e.g. extracted proof data fed to the agent) is sent to the
    # LLM but never rendered — keeps the chat clean of raw JSON dumps.
    if isinstance(msg, dict) and msg.get("_hidden"):
        continue
    # A batch marker renders its interactive card anchored here in the flow.
    bid = msg.get("_batch_id") if isinstance(msg, dict) else None
    if bid:
        batch = next((b for b in st.session_state.batches if b["id"] == bid), None)
        if batch:
            render_batch_table(batch, get_session_erp(),
                               is_active=(bid == st.session_state.active_batch_id))
            continue
        # Batch object gone (e.g. after reload) → fall through to the text summary.

    # An artifact marker renders its document card (PDF download) anchored here.
    aid = msg.get("_artifact_id") if isinstance(msg, dict) else None
    if aid:
        art = next((a for a in st.session_state.artifacts if a.get("_id") == aid), None)
        if art:
            with st.chat_message("assistant"):
                render_artifact(art, aid)
            continue
        # Artifact object gone (e.g. after reload) → fall through to the title text.

    # A three-way marker renders its reconciliation card anchored here in the flow.
    twid = msg.get("_three_way_id") if isinstance(msg, dict) else None
    if twid:
        card = st.session_state.three_way_cards.get(twid)
        if card:
            with st.chat_message("assistant"):
                with st.expander("🔍 Three-Way Reconciliation", expanded=True):
                    render_three_way_card(card, get_session_erp(), conversation)
            continue
        # Card object gone (e.g. after reload) → fall through to the title text.

    # The pending-write confirmation card is anchored to the assistant turn that
    # requested it (so it stays put instead of pinning to the bottom).
    pw = st.session_state.pending_write
    if pw and pw["domain_key"] == domain_key and has_tool_calls(msg):
        tcs = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
        if any(getattr(tc, "id", None) == pw["tool_call_id"] for tc in (tcs or [])):
            with st.chat_message("assistant"):
                render_confirmation_card(pw, get_session_erp(), conversation)
            continue

    if role == "user":
        with st.chat_message("user"):
            img_url = msg_image_url(msg)
            if img_url:
                st.image(img_url, width=360)
            st.markdown(msg_text(msg))
            # Uploaded-proof thumbnails anchored to this user message (bytes live
            # on the batch in session; gone after a full reload → text only).
            tbid = msg.get("_thumbs_batch_id") if isinstance(msg, dict) else None
            if tbid:
                tbatch = next((b for b in st.session_state.batches if b["id"] == tbid), None)
                if tbatch:
                    render_proof_thumbs(tbatch["rows"])
    elif role == "assistant" and msg_text(msg):
        # Show the agent's narration even when the same message also made a tool
        # call — otherwise reconcile→create flows render a card with no words.
        with st.chat_message("assistant"):
            cleaned = strip_thinking(msg_text(msg))
            st.markdown(cleaned)
            render_doc_buttons(cleaned)

# Both the 🔍 three-way card and the 💳 confirmation card now render inline in the
# message loop above, anchored to the turn that produced them (see _three_way_id
# marker and the pending_write tool-call match) — no longer pinned to the bottom.

stt_prompt = st.session_state.stt_input
if stt_prompt:
    st.session_state.stt_input = None

# ── Voice input: a mic icon overlaid inside the chat input (ChatGPT-style) ────
# The component renders in the normal flow; the JS positioner below measures the
# real chat-input bar and pins the mic just left of the Send button, re-running
# on every resize/layout change so it stays aligned on any screen or zoom level.
st.markdown(
    """
    <style>
      /* leave room so typed text never slides under the mic / send button */
      [data-testid="stChatInput"] textarea { padding-right: 3.4rem; }
      /* collapse the mic component's slot in the page flow */
      [data-testid="stElementContainer"]:has(iframe[title$="speech_input"]) {
        height: 0 !important; margin: 0 !important; padding: 0 !important;
        min-height: 0 !important; overflow: visible !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)
_voice_transcript = _speech_input(
    key=f"speech_input_widget_{st.session_state._stt_widget_gen}", default=None)
if _voice_transcript and _voice_transcript.strip():
    # Don't auto-send: stash the text and reset the mic. On the next run it gets
    # *appended* into the chat input box so the user can review/edit before sending.
    st.session_state._stt_pending_text = _voice_transcript.strip()
    st.session_state._stt_widget_gen += 1
    st.rerun()

# Live positioner — runs in the parent document (same-origin srcdoc) and keeps
# the mic glued to the chat input's right side regardless of window size.
components.html(
    """
    <script>
      const doc = window.parent.document;
      function place() {
        const mic   = doc.querySelector('iframe[title$="speech_input"]');
        const input = doc.querySelector('[data-testid="stChatInput"]');
        if (!mic || !input) return;
        const r    = input.getBoundingClientRect();
        // the Send button is the right-most button (a "+" attach button sits left)
        const btns = Array.from(input.querySelectorAll('button'));
        const send = btns.length
          ? btns.reduce((a, b) =>
              a.getBoundingClientRect().left > b.getBoundingClientRect().left ? a : b)
          : null;
        const sr   = send ? send.getBoundingClientRect() : null;
        const size = 34;
        const cy   = r.top + r.height / 2;                 // vertical centre
        const rightAnchor = sr ? sr.left : r.right - 10;   // just left of Send
        mic.style.position   = 'fixed';
        mic.style.width      = size + 'px';
        mic.style.height     = size + 'px';
        mic.style.top        = (cy - size / 2) + 'px';
        mic.style.left       = (rightAnchor - size - 6) + 'px';
        mic.style.zIndex     = '1000000';
        mic.style.background  = 'transparent';
        mic.style.border     = 'none';
      }
      place();
      window.parent.addEventListener('resize', place);
      const mo = new window.parent.MutationObserver(place);
      mo.observe(doc.body, { subtree: true, childList: true, attributes: true });
      setInterval(place, 400);
    </script>
    """,
    height=0,
)

# Freshly transcribed speech → append it into the chat input box (not auto-send).
# Runs in the parent document and writes straight into the chat <textarea>, then
# dispatches a React-friendly input event so Streamlit registers the new value.
_pending_voice = st.session_state.pop("_stt_pending_text", None)
if _pending_voice:
    components.html(
        f"""
        <script>
          const doc = window.parent.document;
          const txt = {json.dumps(_pending_voice)};
          const NONCE = {st.session_state._stt_widget_gen};   /* forces reload */
          function inject() {{
            const ta = doc.querySelector('[data-testid="stChatInput"] textarea');
            if (!ta) return false;
            const sep = (ta.value && !ta.value.endsWith(' ')) ? ' ' : '';
            const setter = Object.getOwnPropertyDescriptor(
              window.parent.HTMLTextAreaElement.prototype, 'value').set;
            setter.call(ta, ta.value + sep + txt);
            // notify React so the value sticks and Send enables
            ta.dispatchEvent(new window.parent.Event('input', {{ bubbles: true }}));
            ta.focus();
            ta.selectionStart = ta.selectionEnd = ta.value.length;
            return true;
          }}
          if (!inject()) {{
            let n = 0;
            const iv = setInterval(() => {{ if (inject() || ++n > 25) clearInterval(iv); }}, 100);
          }}
        </script>
        """,
        height=0,
    )

chat_val = st.chat_input(
    f"Ask {selected_staff}…  (or attach payment proofs)",
    accept_file="multiple",
    file_type=["pdf", "png", "jpg", "jpeg", "tiff", "bmp"],
)

uploaded_files = list(chat_val.files) if (chat_val and chat_val.files) else []
typed_text     = chat_val.text if chat_val else None

# Files attached → reconcile. A SINGLE proof is handled by the agent (dynamic
# tool orchestration); MULTIPLE proofs use the deterministic one-pass batch.
if uploaded_files:
    bank_month = (month or "May2026").strip()
    sheet      = (sheet_url or config.BANK_STATEMENT_SHEET_URL)

if uploaded_files and len(uploaded_files) == 1:
    f = uploaded_files[0]
    try:
        file_bytes = f.getvalue()
    except Exception:
        file_bytes = f.read()

    # Keep any text the user typed alongside the upload — it's their instruction.
    typed = (typed_text or "").strip()
    note  = f"📎 Uploaded payment proof: `{f.name}`"
    if typed:
        note = f"{note}\n\n{typed}"

    # Embed the image in the message so it re-renders on every rerun (and survives
    # reload via the DB). Without this the proof only shows live during the upload
    # turn and vanishes on the next message. PDFs have no inline preview.
    ext = Path(f.name).suffix.lstrip(".").lower()
    is_image = ext in ("png", "jpg", "jpeg", "tiff", "bmp")
    if is_image:
        import base64
        mime = f"image/{ext.replace('jpg', 'jpeg')}"
        data_url = f"data:{mime};base64,{base64.b64encode(file_bytes).decode()}"
        user_msg = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": note},
        ]}
    else:
        user_msg = {"role": "user", "content": note}

    st.session_state.tool_trace = []   # fresh trace per uploaded proof (resume keeps it)
    with st.chat_message("user"):
        if is_image:
            st.image(data_url, width=360)
        st.markdown(note)
    conversation.append(user_msg)
    save_message(st.session_state.session_id, domain_key, "user", note,
                 image_bytes=(file_bytes if is_image else None), image_ext=ext or "png")
    update_session_title(st.session_state.session_id, typed or f.name)

    # Deterministic extraction (perception), then hand the structured data to the
    # agent as a HIDDEN message — the chat shows only the clean upload bubble.
    with st.spinner(f"Reading {f.name}…"):
        try:
            data = extract_payment_receipt(file_bytes, f.name)
            st.session_state.proof_bytes    = file_bytes
            st.session_state.proof_filename = f.name
            # Keep ONLY the original cited reference for later override detection. (Do NOT set
            # proof_data here — that triggers the manual "Review Extracted Payment Receipt" form.)
            st.session_state.proof_orig_ref = data.get("reference_no")
            hidden = (
                "A payment proof was uploaded and its fields were extracted "
                "(do NOT echo this raw data back verbatim):\n"
                f"{json.dumps(data, ensure_ascii=False)}\n"
                f"Bank statement month: {bank_month}. Sheet URL: {sheet}\n"
                "Reconcile it now: call three_way_reconcile with these fields, then "
                "explain the outcome and the next step to the user."
                + (f"\nAlso address the user's note: {typed}" if typed else "")
            )
        except Exception as e:
            hidden = (f"A payment proof '{f.name}' was uploaded but extraction failed: {e}. "
                      "Apologise and ask the user to re-upload a clearer image.")

    conversation.append({"role": "user", "content": hidden, "_hidden": True})
    run_agent_loop(init_llm(st.session_state.model_key), get_session_erp(),
                   conversation, domain_tools, domain_key)
    st.rerun()   # prevent the typed text from being re-processed by the prompt block below

# Multiple files → deterministic batch, narrating progress live.
elif uploaded_files:
    with st.chat_message("user"):
        st.markdown(
            f"📎 Uploaded **{len(uploaded_files)}** payment proof(s): "
            + ", ".join(f"`{f.name}`" for f in uploaded_files)
        )
        render_upload_thumbs(uploaded_files)
    with st.chat_message("assistant"):
        st.markdown(f"Got it — let me go through these {len(uploaded_files)} one by one.")
        status = st.status("Processing payment proofs…", expanded=True)
        bar = st.progress(0.0)

        def _narrate(done, total, message):
            status.write(message)
            bar.progress(done / total)

        rows = process_proof_batch(
            uploaded_files, bank_month, sheet, get_session_erp(), on_event=_narrate)

        n_ready = sum(1 for r in rows if r.get("can_create"))
        status.update(label=f"Done — {len(uploaded_files)} processed, {n_ready} ready to create",
                      state="complete", expanded=False)

    # Register the batch and make it the active (interactive) card. A marker
    # message anchors its card in the chat flow; the markdown summary is also
    # saved to the DB so it survives reload and the agent can explain each proof.
    bid = __import__("uuid").uuid4().hex[:8]
    st.session_state.batches.append({"id": bid, "rows": rows, "ver": 0})
    st.session_state.active_batch_id = bid

    upload_note = (f"📎 Uploaded {len(uploaded_files)} payment proof(s): "
                   + ", ".join(f.name for f in uploaded_files))
    summary_md = _batch_summary_markdown(rows)
    conversation.append({"role": "user", "content": upload_note, "_thumbs_batch_id": bid})
    conversation.append({"role": "assistant", "content": summary_md, "_batch_id": bid})
    save_message(st.session_state.session_id, domain_key, "user", upload_note)
    save_message(st.session_state.session_id, domain_key, "assistant", summary_md)
    update_session_title(st.session_state.session_id, f"{len(uploaded_files)} payment proofs")
    st.rerun()

prompt = typed_text or stt_prompt

if prompt and prompt.strip():
    prompt = prompt.strip()
    # A new turn has started — prior batch cards become read-only.
    st.session_state.active_batch_id = None
    st.session_state.tool_trace = []   # fresh trace per question (resume keeps it)
    with st.chat_message("user"):
        st.markdown(prompt)
    conversation.append({"role": "user", "content": prompt})
    save_message(st.session_state.session_id, domain_key, "user", prompt)
    # Use first user message as session title
    user_msgs = [m for m in conversation if (m["role"] if isinstance(m, dict) else m.role) == "user"]
    if len(user_msgs) == 1:
        update_session_title(st.session_state.session_id, prompt)
    # Guardrail: reject injection / harmful requests before the agent loop runs
    # (intent monitoring — "what it should not do"). Recent turns are passed as
    # context so short follow-ups aren't misjudged in isolation.
    recent = [{"role": msg_role(m), "content": msg_text(m)}
              for m in conversation[:-1]
              if not (isinstance(m, dict) and m.get("_hidden")) and msg_text(m)]
    verdict = guardrail.check_request(prompt, history=recent)
    if not verdict["allow"]:
        refusal = ("🛡️ I can't help with that." + (f" {verdict['reason']}" if verdict["reason"] else ""))
        with st.chat_message("assistant"):
            st.markdown(refusal)
        conversation.append({"role": "assistant", "content": refusal})
        save_message(st.session_state.session_id, domain_key, "assistant", refusal)
    else:
        run_agent_loop(init_llm(st.session_state.model_key), get_session_erp(), conversation, domain_tools, domain_key)

elif st.session_state.resume_agent:
    st.session_state.resume_agent = False
    run_agent_loop(init_llm(st.session_state.model_key), get_session_erp(), conversation, domain_tools, domain_key)
