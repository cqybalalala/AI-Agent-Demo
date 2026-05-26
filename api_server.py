"""
FastAPI server — exposes the multi-agent loop as an HTTP API.
Designed for the ERPNext sidebar POC: receives a query + optional doc_context
(the current ERPNext form data) and returns the agent's answer.

Run:
    pip install fastapi uvicorn
    uvicorn api_server:app --host 0.0.0.0 --port 8001 --reload
"""

import json
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
from agent import LLMAdapter, run_turn
from erpnext_client import get_erp_adapter
from tools import get_tools_for_domain
from domains import get_domain_config
from router import route
from rag import inject_rag_context


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="ERPNext AI Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # POC: allow all. Restrict in production.
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared singletons — created once on startup
llm = LLMAdapter()
erp = get_erp_adapter()

# In-memory session store: session_id → { domain_key → conversation list }
# Each browser tab gets its own session_id so histories don't bleed across tabs.
sessions: dict[str, dict] = {}


# ── Request / Response models ──────────────────────────────────────────────────

class ChatRequest(BaseModel):
    query: str
    session_id: Optional[str] = None   # pass back the id you got from /session
    doc_context: Optional[dict] = None  # current ERPNext form: {doctype, docname, doc}


class ChatResponse(BaseModel):
    answer: str
    domain: str
    session_id: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Quick ping — check from ERPNext JS before sending a real query."""
    return {"status": "ok", "model": config.AGENT_MODEL}


@app.post("/session")
def new_session():
    """Create a new conversation session. Call once per browser tab / page load."""
    sid = str(uuid.uuid4())
    sessions[sid] = {}
    return {"session_id": sid}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    # ── Session ──────────────────────────────────────────────────────────────
    sid = req.session_id or str(uuid.uuid4())
    if sid not in sessions:
        sessions[sid] = {}
    session = sessions[sid]

    # ── Route to domain ───────────────────────────────────────────────────────
    domain_key = route(llm, req.query)
    domain_cfg = get_domain_config(domain_key)
    domain_tools = get_tools_for_domain(domain_cfg["read_tools"], domain_cfg["write_tools"])

    # ── Build / retrieve conversation for this domain ─────────────────────────
    if domain_key not in session:
        system_prompt = domain_cfg["system_prompt"]

        # Inject doc_context into the system prompt when available
        if req.doc_context:
            system_prompt += _format_doc_context(req.doc_context)

        session[domain_key] = [{"role": "system", "content": system_prompt}]

    conversation = session[domain_key]

    # ── Augment query with RAG schema hints ───────────────────────────────────
    augmented_query = inject_rag_context(req.query)

    # If doc_context is provided and this is the first user message, prepend it
    # as a one-shot context reminder so the agent always knows what's on screen.
    if req.doc_context:
        context_note = _format_doc_context_inline(req.doc_context)
        augmented_query = f"{context_note}\n\nUser question: {augmented_query}"

    conversation.append({"role": "user", "content": augmented_query})

    # ── Run agent ─────────────────────────────────────────────────────────────
    answer = run_turn(llm, erp, conversation, domain_tools)

    return ChatResponse(answer=answer, domain=domain_cfg["name"], session_id=sid)


@app.delete("/session/{session_id}")
def clear_session(session_id: str):
    """Clear conversation history for a session (equivalent to CLI 'clear')."""
    if session_id in sessions:
        sessions.pop(session_id)
    return {"cleared": session_id}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_doc_context(ctx: dict) -> str:
    """Append doc context to the system prompt (injected once at conversation start)."""
    doctype = ctx.get("doctype", "Unknown")
    docname = ctx.get("docname", "Unknown")
    doc     = ctx.get("doc", {})

    # Pull a few key fields so the system prompt isn't bloated with the full doc
    summary_fields = [
        "status", "docstatus", "customer", "supplier",
        "grand_total", "outstanding_amount", "currency",
        "posting_date", "due_date", "transaction_date",
    ]
    summary = {k: doc[k] for k in summary_fields if k in doc}

    return (
        f"\n\n## Current ERPNext Document\n"
        f"- DocType: {doctype}\n"
        f"- Name: {docname}\n"
        f"- Key fields: {json.dumps(summary, ensure_ascii=False)}\n"
        f"The user is currently viewing this document. Use it as context for their question."
    )


def _format_doc_context_inline(ctx: dict) -> str:
    """Short one-liner prepended to each user message so the agent remembers context."""
    doctype = ctx.get("doctype", "")
    docname = ctx.get("docname", "")
    if doctype and docname:
        return f"[Context: user is viewing {doctype} {docname}]"
    return ""
