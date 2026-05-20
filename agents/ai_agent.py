"""
AI agent — generates LangChain integration code (if the architecture asks for
AI) and config files (always).

Reads:
  - state["architecture"] — used to decide if AI features are needed.
  - state["backend_code"]["main.py"] — passed to the LLM so any generated
    ai/chat_handler.py matches the import contract the backend expects.

Writes:
  - state["ai_code"]     — {filename: code}, empty dict when no AI is needed.
  - state["config_code"] — {filename: code}, always contains docker-compose.yml
    and .env.example.
"""

import json
import re
from pathlib import Path
from typing import List

from core.llm_factory import get_llm
from core.state import AgentState


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = PROJECT_ROOT / "prompts" / "ai_agent.md"

# Word-bounded match so we don't trigger on substrings like "generated" or
# "champion". These are the signals that the user wanted an AI feature.
_AI_KEYWORDS_RE = re.compile(
    r"\b(chat|llm|generate|assistant|completion|prompt|embedding|rag)\b",
    re.IGNORECASE,
)


def _load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _build_default_env_example(needs_ai: bool) -> str:
    """
    Deterministic fallback contents for .env.example. The LLM is unreliable
    about emitting this file, but its contents are fully predictable from
    needs_ai — so when it's missing we drop in a known-good version rather
    than retry the model.
    """
    lines = ["BACKEND_PORT=8000", "FRONTEND_PORT=3000"]
    if needs_ai:
        lines.append("OLLAMA_MODEL=qwen2.5-coder:7b")
        lines.append("OLLAMA_BASE_URL=http://localhost:11434")
    return "\n".join(lines) + "\n"


def _has_ai_feature(architecture: dict) -> bool:
    """Decide whether the architecture calls for an AI/LangChain layer.

    Primary signal: the orchestrator includes an "ai" key in tech_stack only
    when the app needs it. Secondary signal: endpoint paths/descriptions
    contain AI-related keywords (covers cases where the orchestrator forgot
    the tech_stack entry but listed e.g. a `/api/chat` route).
    """
    tech_stack = architecture.get("tech_stack", {}) or {}
    if any(k.lower() == "ai" for k in tech_stack.keys()):
        return True

    for ep in architecture.get("api_endpoints", []) or []:
        blob = f"{ep.get('path', '')} {ep.get('description', '')}"
        if _AI_KEYWORDS_RE.search(blob):
            return True

    return False


def _build_human_message(
    architecture: dict, backend_code: dict, needs_ai: bool
) -> str:
    """Brief the LLM with the arch, the backend main.py, and the AI flag."""
    tech_stack = architecture.get("tech_stack", {})
    endpoints = architecture.get("api_endpoints", []) or []

    endpoints_block = "\n".join(
        f"- {ep['method']} {ep['path']} — {ep['description']}" for ep in endpoints
    ) or "- (none)"

    backend_main = backend_code.get("main.py", "")
    if not isinstance(backend_main, str):
        backend_main = ""

    return (
        f"needs_ai: {str(needs_ai).lower()}\n\n"
        f"## Tech Stack\n{json.dumps(tech_stack, indent=2)}\n\n"
        f"## API Endpoints\n{endpoints_block}\n\n"
        "## Existing backend main.py (match imports if generating AI code)\n"
        "```\n"
        f"{backend_main}\n"
        "```\n\n"
        "Return only the JSON object — no fences, no commentary."
    )


def ai_agent(state: AgentState) -> AgentState:
    """Populate state["ai_code"] and state["config_code"]."""
    architecture = state["architecture"]
    backend_code = state.get("backend_code", {}) or {}

    needs_ai = _has_ai_feature(architecture)

    llm = get_llm(temperature=0.1).bind(format="json")
    messages = [
        ("system", _load_system_prompt()),
        ("human", _build_human_message(architecture, backend_code, needs_ai)),
    ]

    response = llm.invoke(messages)
    raw_text = response.content if hasattr(response, "content") else str(response)
    parsed = json.loads(raw_text)

    # Hard guarantee: ai_code is empty whenever no AI feature was detected,
    # regardless of what the LLM returned.
    state["ai_code"] = parsed.get("ai_code", {}) if needs_ai else {}

    # Deterministic safety net: .env.example must always exist in config_code.
    # The LLM frequently drops it; its contents are fully predictable, so we
    # synthesize it locally when missing instead of retrying the model.
    config_code = parsed.get("config_code", {}) or {}
    if ".env.example" not in config_code:
        config_code[".env.example"] = _build_default_env_example(needs_ai)
    state["config_code"] = config_code

    return state
