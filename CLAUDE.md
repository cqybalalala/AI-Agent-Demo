# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A multi-agent AI assistant for ERPNext. Users ask natural-language questions; the system routes them to specialized domain agents that call ERPNext REST APIs and return answers grounded in live data.

## Running the Agent

```bash
pip install openai httpx
python agent.py
```

In-session commands: `clear` (reset history), `exit`/`quit`/`q` (exit).

## Architecture

```
User Query
    ↓
router.py  (keyword-first classification, LLM fallback)
    ↓
domains.py  (domain config: AR / AP / Sales / Procurement / General)
    ↓
agent.py: run_turn()  (multi-step tool-calling loop, up to MAX_TOOL_LOOPS)
    ↓
tools.py: execute_tool()  (dispatches to ERPNextAdapter methods)
    ↓
erpnext_client.py: ERPNextAdapter  (httpx calls to Frappe REST API)
```

### Key modules

- **`config.py`** — All runtime settings: Ollama URL/model, ERPNext URL/credentials, `AGENT_READ_ONLY`, `MAX_TOOL_LOOPS`, `RESULT_LIMIT`.
- **`router.py`** — `route()` tries `keyword_route()` first; falls back to `llm_route()` only when keywords don't match.
- **`domains.py`** — Each domain defines its name, keywords, read/write tool lists, and a system prompt builder. Adding a domain means adding an entry here.
- **`tools.py`** — Tool JSON schemas sent to the LLM, plus `execute_tool()` dispatcher. Tools: `erpnext_list`, `erpnext_get`, `erpnext_report`, `erpnext_linked`, `erpnext_items`, `erpnext_search`, `erpnext_fields`, `erpnext_docs`, `execute_python`, `erpnext_create`.
- **`erpnext_client.py`** — `ERPAdapter` abstract base; `ERPNextAdapter` concrete implementation. `list()` supports server-side aggregation (`group_by` + `sum_field`). `linked()` tries multiple field naming patterns to work around ERPNext schema variation. `get_erp_adapter()` is the factory.
- **`agent.py`** — `LLMAdapter` wraps Ollama via OpenAI-compatible client. `run_turn()` runs the tool loop and handles Qwen3 thinking-mode stripping. Write operations trigger a `Proceed? (y/n)` confirmation gate before calling `erpnext_create`.

### Domain tool isolation

Each domain only sees a subset of tools. AR/AP/Sales/Procurement each have a curated read-tool list and conditionally expose `erpnext_create` (write) when `AGENT_READ_ONLY = False`. The General domain has access to all tools.

### ERPNext report reference

`docs/reports.md` documents the correct filter keys and parameter values for each built-in ERPNext report (Purchase/Sales Analytics, Gross Profit, AR/AP Aging, Item History). Consult this before constructing `erpnext_report` tool calls.

## Extension Points

- **New domain:** Add entry to `domains.py` with keywords, tool lists, and system prompt.
- **New tool:** Add JSON schema to `tools.py`, add handler branch in `execute_tool()`, add method to `ERPNextAdapter` if needed.
- **Swap LLM:** Replace `LLMAdapter` in `agent.py` (currently OpenAI-compatible Ollama).
- **Swap ERP backend:** Implement a new class inheriting `ERPAdapter` in `erpnext_client.py` and update `get_erp_adapter()`.
