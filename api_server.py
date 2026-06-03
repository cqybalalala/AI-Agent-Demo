"""
FastAPI server — exposes the ERP multi-agent loop as a streaming SSE API.
Bridges the Next.js chatbot frontend to the Python agent/tools/ERPNext stack.

Run:
    uvicorn api_server:app --host 0.0.0.0 --port 8001 --reload
"""

import asyncio
import base64
import json
import uuid
from functools import partial
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
from agent import LLMAdapter, strip_thinking
from domains import get_domain_config
from erpnext_client import get_erp_adapter
from router import route
from tools import execute_tool, get_tools_for_domain

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="ERPNext AI Agent API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Singletons
llm = LLMAdapter()
erp = get_erp_adapter()

# Session store: session_id → { domain_key → conversation, "pending_write" → ... }
sessions: dict[str, dict] = {}

# Tools that must NOT be auto-executed — require human confirmation
WRITE_TOOL_NAMES = {"erpnext_create", "erpnext_update", "create_payment_entry"}


# ── Request models ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    query: str
    session_id: Optional[str] = None
    doc_context: Optional[dict] = None
    model_key: Optional[str] = None  # e.g. "Qwen3-VL 32B · OpenRouter"
    # [{url: "data:image/...", name: "...", content_type: "..."}]
    attachments: Optional[list[dict]] = None


class ConfirmWriteRequest(BaseModel):
    session_id: str
    confirmed: bool
    reason: Optional[str] = None


# ── SSE helper ─────────────────────────────────────────────────────────────────

def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── Agent stream ───────────────────────────────────────────────────────────────

async def run_agent_stream(
    session_id: str,
    domain_key: str,
    conversation: list,
    tools: list,
    llm_instance: LLMAdapter,
) -> AsyncGenerator[str, None]:
    """Runs the multi-turn tool-calling loop; yields SSE events for each step."""
    loop = asyncio.get_event_loop()

    for _ in range(config.MAX_TOOL_LOOPS):
        # Blocking LLM call — run in thread pool so we don't block the event loop
        try:
            response = await loop.run_in_executor(
                None, partial(llm_instance.chat, conversation, tools)
            )
        except Exception as exc:
            yield sse({"type": "error", "message": str(exc)})
            return

        msg = response.choices[0].message
        # Append the raw message object (OpenAI SDK type) to conversation
        conversation.append(msg)

        # No tool calls → final answer
        if not msg.tool_calls:
            content = strip_thinking(msg.content or "")
            yield sse({"type": "text", "content": content})
            yield sse({"type": "done"})
            return

        # Process each tool call
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            tool_name = tc.function.name

            yield sse({"type": "tool_status", "tool": tool_name, "status": "running"})

            # ── HITL gate ──────────────────────────────────────────────────────
            if tool_name in WRITE_TOOL_NAMES:
                # Save pending write to session so /confirm-write can resume
                sessions[session_id]["pending_write"] = {
                    "tool": tool_name,
                    "args": args,
                    "tool_call_id": tc.id,
                    "domain_key": domain_key,
                }
                yield sse({
                    "type": "confirmation_required",
                    "tool": tool_name,
                    "args": args,
                    "tool_call_id": tc.id,
                })
                return  # Pause — /confirm-write will resume

            # ── Execute read tool ──────────────────────────────────────────────
            try:
                result = await loop.run_in_executor(
                    None, partial(execute_tool, tool_name, args, erp)
                )
            except Exception as exc:
                result = {"error": str(exc)}

            yield sse({"type": "tool_status", "tool": tool_name, "status": "done"})

            conversation.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    yield sse({"type": "error", "message": "Reached tool call limit — please rephrase."})


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "model": config.AGENT_MODEL}


@app.post("/session")
def new_session():
    sid = str(uuid.uuid4())
    sessions[sid] = {}
    return {"session_id": sid}


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    sid = req.session_id or str(uuid.uuid4())
    if sid not in sessions:
        sessions[sid] = {}
    session = sessions[sid]

    # Pick LLM for this request (fall back to default singleton)
    model_cfg = config.MODELS.get(req.model_key) if req.model_key else None
    request_llm = LLMAdapter(model_cfg) if model_cfg else llm

    # Store model key in session so /confirm-write can reuse it
    session["model_key"] = req.model_key

    # Route to domain (blocking, run in thread pool)
    loop = asyncio.get_event_loop()
    domain_key = await loop.run_in_executor(None, partial(route, request_llm, req.query))
    domain_cfg = get_domain_config(domain_key)
    domain_tools = get_tools_for_domain(domain_cfg["read_tools"], domain_cfg["write_tools"])

    # Create conversation if first message in this domain
    if domain_key not in session:
        session[domain_key] = [{"role": "system", "content": domain_cfg["system_prompt"]}]

    conversation = session[domain_key]

    # Build user message — include image attachments if any
    if req.attachments:
        content_parts = [
            {"type": "image_url", "image_url": {"url": att["url"]}}
            for att in req.attachments
            if att.get("content_type", "").startswith("image/")
        ]
        content_parts.append({"type": "text", "text": req.query})
        conversation.append({"role": "user", "content": content_parts})
    else:
        conversation.append({"role": "user", "content": req.query})

    async def generate():
        yield sse({"type": "session_id", "session_id": sid})
        yield sse({"type": "domain", "domain": domain_cfg["name"]})
        async for event in run_agent_stream(sid, domain_key, conversation, domain_tools, request_llm):
            yield event

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/confirm-write")
async def confirm_write(req: ConfirmWriteRequest):
    session = sessions.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    pending = session.get("pending_write")
    if not pending:
        raise HTTPException(status_code=400, detail="No pending write operation")

    domain_key = pending["domain_key"]
    conversation = session[domain_key]
    tool_call_id = pending["tool_call_id"]
    session.pop("pending_write", None)

    loop = asyncio.get_event_loop()

    if req.confirmed:
        try:
            result = await loop.run_in_executor(
                None, partial(execute_tool, pending["tool"], pending["args"], erp)
            )
        except Exception as exc:
            result = {"error": str(exc)}
    else:
        result = {"cancelled": True, "reason": req.reason or "User declined"}

    conversation.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(result, ensure_ascii=False),
    })

    domain_cfg = get_domain_config(domain_key)
    domain_tools = get_tools_for_domain(domain_cfg["read_tools"], domain_cfg["write_tools"])

    # Reuse the same model that was active when the write was triggered
    saved_model_key = session.get("model_key")
    model_cfg = config.MODELS.get(saved_model_key) if saved_model_key else None
    resume_llm = LLMAdapter(model_cfg) if model_cfg else llm

    async def generate():
        async for event in run_agent_stream(req.session_id, domain_key, conversation, domain_tools, resume_llm):
            yield event

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/extract-file")
async def extract_file(file: UploadFile = File(...)):
    """Extract structured data from a payment proof or invoice (image / PDF)."""
    from invoice_extractor import extract_payment_receipt

    content = await file.read()
    b64 = base64.b64encode(content).decode()
    data_url = f"data:{file.content_type};base64,{b64}"

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, partial(extract_payment_receipt, data_url, file.filename)
    )
    return result


@app.delete("/session/{session_id}")
def clear_session(session_id: str):
    sessions.pop(session_id, None)
    return {"cleared": session_id}
