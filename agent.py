"""
Multi-agent loop + CLI entry point.
Router classifies queries → domain agent handles them with focused tools & prompts.
"""
import json
import re
import sys
from openai import OpenAI

import config
from erpnext_client import get_erp_adapter
from tools import TOOL_DEFINITIONS, WRITE_TOOL_DEFINITIONS, get_tools_for_domain, execute_tool
from domains import get_domain_config, DOMAINS
from router import route


# ── LLM Adapter ───────────────────────────────────────────────────────────────

class LLMAdapter:
    """Thin wrapper — replace body to support Claude API, OpenAI, etc."""

    def __init__(self, model_cfg: dict = None):
        cfg = model_cfg or config.MODELS[config.DEFAULT_MODEL]
        self.model = cfg["model"]
        self.extra_body = cfg.get("extra_body")
        self.client = OpenAI(
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            default_headers={"HTTP-Referer": "http://localhost:8501", "X-Title": "Treasury Agent"},
        )

    def chat(self, messages: list, tools: list) -> object:
        kwargs = dict(model=self.model, messages=messages, temperature=config.AGENT_TEMPERATURE)
        if tools:
            kwargs["tools"] = tools
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        return self.client.chat.completions.create(**kwargs)


# ── Helpers ───────────────────────────────────────────────────────────────────

def strip_thinking(text: str) -> str:
    """Print qwen3 thinking block in dim color, return clean answer."""
    def show_think(m):
        thought = m.group(1).strip()
        if thought:
            print(f"\033[90m[thinking]\n{thought}\n[/thinking]\033[0m\n")
        return ""
    return re.sub(r"<think>(.*?)</think>", show_think, text, flags=re.DOTALL).strip()


def print_tool_call(name: str, args: dict):
    arg_str = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
    print(f"  \033[90m→ {name}({arg_str})\033[0m")


# ── Agent loop ────────────────────────────────────────────────────────────────

def run_turn(llm: LLMAdapter, erp, conversation: list, tools: list) -> str:
    """Run one user turn: may call tools multiple times before final answer."""
    for _ in range(config.MAX_TOOL_LOOPS):
        response = llm.chat(conversation, tools)
        msg = response.choices[0].message
        conversation.append(msg)

        if not msg.tool_calls:
            return strip_thinking(msg.content or "")

        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            print_tool_call(tc.function.name, args)

            result = execute_tool(tc.function.name, args, erp)
            conversation.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      json.dumps(result, ensure_ascii=False),
            })

    return "Reached tool call limit — please rephrase your question."


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    llm = LLMAdapter()
    erp = get_erp_adapter()

    print(f"ERPNext Multi-Agent  |  model: {config.AGENT_MODEL}")
    print(f"ERP: {config.ERPNEXT_URL}  |  domains: {', '.join(DOMAINS.keys())}")
    print("Type 'exit' to quit, 'clear' to reset conversation.\n")

    # Per-domain conversation histories
    conversations = {}

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            break
        if user_input.lower() == "clear":
            conversations = {}
            print("All conversations cleared.\n")
            continue

        # Route to domain
        domain_key = route(llm, user_input)
        domain_cfg = get_domain_config(domain_key)
        domain_tools = get_tools_for_domain(domain_cfg["read_tools"], domain_cfg["write_tools"])

        print(f"\033[36m[{domain_cfg['name']}]\033[0m")

        # Get or create domain conversation
        if domain_key not in conversations:
            conversations[domain_key] = [
                {"role": "system", "content": domain_cfg["system_prompt"]}
            ]

        conversations[domain_key].append({"role": "user", "content": user_input})
        
        answer = run_turn(llm, erp, conversations[domain_key], domain_tools)
        print(f"\nAgent: {answer}\n")


if __name__ == "__main__":
    main()
