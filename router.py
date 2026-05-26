"""
Router — classifies user queries to the correct domain agent.
Uses keyword matching first (fast, no LLM call), falls back to LLM classification.
"""
import json
from domains import DOMAINS


def keyword_route(user_message: str) -> str | None:
    """Try to match user message to a domain via keywords. Returns domain key or None."""
    msg_lower = user_message.lower()
    scores = {}
    for key, domain in DOMAINS.items():
        if key == "general":
            continue
        score = sum(1 for kw in domain["keywords"] if kw in msg_lower)
        if score > 0:
            scores[key] = score
    if scores:
        return max(scores, key=scores.get)
    return None


def llm_route(llm, user_message: str) -> str:
    """Use the LLM to classify which domain should handle the query."""
    domain_descriptions = "\n".join(
        f"- {key}: {d['description']}"
        for key, d in DOMAINS.items()
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are a query router. Classify the user's message into exactly one domain.\n"
                "Reply with ONLY the domain key (one word), nothing else.\n\n"
                f"Domains:\n{domain_descriptions}"
            ),
        },
        {"role": "user", "content": user_message},
    ]
    response = llm.chat(messages, tools=[])
    answer = (response.choices[0].message.content or "").strip().lower()
    # Extract domain key from response
    for key in DOMAINS:
        if key in answer:
            return key
    return "general"


def route(llm, user_message: str) -> str:
    """Route a user message to the best domain agent. Returns domain key."""
    # Fast path: keyword matching
    result = keyword_route(user_message)
    if result:
        return result
    # Slow path: LLM classification
    return llm_route(llm, user_message)
