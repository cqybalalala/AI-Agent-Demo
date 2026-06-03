import os
try:
    from dotenv import load_dotenv
    load_dotenv()   # load secrets from a local .env file (git-ignored, never committed)
except ImportError:
    pass

# ── Secrets (read from .env / environment — never hard-coded or committed) ─────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
CHUTES_API_KEY     = os.getenv("CHUTES_API_KEY", "")

# ── LLM ──────────────────────────────────────────────────────────────────────
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OLLAMA_BASE_URL = "http://10.10.18.180:11434"
AGENT_MODEL     = "qwen/qwen3-vl-32b-instruct"
EMBED_MODEL     = "bge-m3:latest"   # reserved for RAG tool selection (later)

# Selectable models (shown in the UI picker). Each needs vision + tool-calling.
MODELS = {
    "Qwen3.6-27B · Chutes (sponsor)": {
        "base_url": "https://llm.chutes.ai/v1",
        "api_key":  CHUTES_API_KEY,
        "model":    "Qwen/Qwen3.6-27B-TEE",
        # This is a reasoning model — disable thinking, else every turn wastes
        # hundreds of tokens (and time) on a hidden reasoning block.
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    },
    "Qwen3-VL 32B · OpenRouter": {
        "base_url": OPENROUTER_BASE_URL,
        "api_key":  OPENROUTER_API_KEY,
        "model":    "qwen/qwen3-vl-32b-instruct",
    },
    "Qwen3-VL 32B · Ollama (LAN)": {
        "base_url": OLLAMA_BASE_URL.rstrip("/") + "/v1",
        "api_key":  "ollama",
        "model":    "qwen/qwen3-vl-32b-instruct",
    },
}
DEFAULT_MODEL = "Qwen3-VL 32B · OpenRouter"   # fast + reliable; recommended for reviewers

# ── ERP ──────────────────────────────────────────────────────────────────────
ERP_PROVIDER    = "erpnext"         # swap to "odoo" / "sap" later
ERPNEXT_URL     = "https://deluxe-obj-minnesota-hardcover.trycloudflare.com"
ERPNEXT_API_KEY = "8203688a6f18fa4"
ERPNEXT_SECRET  = "9b34ec3b96f507e"

# ── ERP metadata ─────────────────────────────────────────────────────────────
ERPNEXT_COMPANY    = "Penang Components Sdn Bhd"

# GL accounts used when posting a Payment Entry. This instance has NO `Bank Account`
# doctype records and NO `Currency Exchange` records, so the bank line and the cost
# center are addressed directly by their GL account names (verified on the live ERP).
BANK_GL_ACCOUNT      = "1210 - Maybank - PCSB"          # Bank-type GL account (paid_to)
BANK_CHARGES_ACCOUNT = "5221 - Bank Charges - PCSB"     # Expense — the SWIFT/TT fee line
EXCHANGE_GL_ACCOUNT  = "5219 - Exchange Gain/Loss - PCSB"  # realized forex gain/loss
COST_CENTER          = "Main - PCSB"

# ── Bank Statement Google Sheet ───────────────────────────────────────────────
# Paste your Google Sheet URL here (must be set to "Anyone with link can view")
BANK_STATEMENT_SHEET_URL = "https://docs.google.com/spreadsheets/d/1gTz-uJPGNDNkhP_rtZMWUfVO_duphO3lqnAInOgG8ys/edit?usp=sharing"

# ── Agent behaviour ───────────────────────────────────────────────────────────
AGENT_READ_ONLY  = True   # set False to enable write tools (later)
AGENT_TEMPERATURE = 0.3   # low = less hallucination, high = more creative
MAX_TOOL_LOOPS   = 10     # prevent infinite tool call loops
RESULT_LIMIT     = 30     # max rows returned per list call

# ── Guardrail (intent / safety pre-check before the agent loop) ───────────────
# A small model classifies each user request as allow/block. Primary runs on the
# LAN Ollama (cheap + private); if it's unreachable we fall back to Chutes.
GUARDRAIL_ENABLED  = True
GUARDRAIL_PRIMARY = {
    "base_url":   OLLAMA_BASE_URL.rstrip("/") + "/v1",
    "api_key":    "ollama",
    # 8B classifies all categories reliably (4B under-blocks out-of-scope).
    # enable_thinking=False: it's a reasoning model — skip the hidden think block.
    "model":      "qwen3:8b-q4_K_M",
    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    "timeout":    15,
}
GUARDRAIL_FALLBACK = {
    "base_url": "https://llm.chutes.ai/v1",
    "api_key":  CHUTES_API_KEY,
    "model":    "Qwen/Qwen3.6-27B-TEE",
    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    "timeout":  30,
}
