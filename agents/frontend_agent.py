"""
Frontend agent — generates React + Tailwind code from the architecture plan.

Reads state["architecture"] (api_endpoints + frontend_components + tech_stack)
and writes state["frontend_code"] as a {filename: file_contents} dict that
the file_writer node can dump straight to disk.
"""

import json
from pathlib import Path

from core.llm_factory import get_llm
from core.state import AgentState


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = PROJECT_ROOT / "prompts" / "frontend.md"


# Deterministic fallbacks for the three config files the LLM keeps emitting
# in invalid forms (bare object literals, missing module.exports wrapper,
# wrong port). Triggered when the LLM's version is missing or doesn't
# contain "module.exports" — vite.config.js correctly uses ES-module
# syntax and therefore gets force-replaced on every run, which is fine:
# the canonical version below is always correct.
_DEFAULT_POSTCSS_CONFIG = (
    "module.exports = {\n"
    "  plugins: {\n"
    "    tailwindcss: {},\n"
    "    autoprefixer: {}\n"
    "  }\n"
    "}\n"
)

_DEFAULT_TAILWIND_CONFIG = (
    "module.exports = {\n"
    "  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],\n"
    "  theme: { extend: {} },\n"
    "  plugins: []\n"
    "}\n"
)

_DEFAULT_VITE_CONFIG = (
    "import { defineConfig } from 'vite'\n"
    "import react from '@vitejs/plugin-react'\n"
    "export default defineConfig({\n"
    "  plugins: [react()],\n"
    "  server: { port: 3000 }\n"
    "})\n"
)

_CONFIG_FALLBACKS = {
    "postcss.config.js": _DEFAULT_POSTCSS_CONFIG,
    "tailwind.config.js": _DEFAULT_TAILWIND_CONFIG,
    "vite.config.js": _DEFAULT_VITE_CONFIG,
}


def _load_system_prompt() -> str:
    """Read the frontend system prompt at call time so edits don't need a reimport."""
    return PROMPT_PATH.read_text(encoding="utf-8")


def _apply_config_fallbacks(frontend_code: dict) -> None:
    """
    Replace any config file in `frontend_code` whose contents are missing
    or don't contain "module.exports" with the canonical version. Mutates
    the dict in place.
    """
    for filename, fallback in _CONFIG_FALLBACKS.items():
        existing = frontend_code.get(filename)
        if not isinstance(existing, str) or "module.exports" not in existing:
            frontend_code[filename] = fallback


def _build_human_message(architecture: dict) -> str:
    """Pull the fields the frontend cares about out of the architecture plan."""
    endpoints = architecture.get("api_endpoints", [])
    components = architecture.get("frontend_components", [])
    tech_stack = architecture.get("tech_stack", {})

    endpoints_block = "\n".join(
        f"- {ep['method']} {ep['path']} — {ep['description']}" for ep in endpoints
    ) or "- (none)"
    components_block = "\n".join(f"- {c}" for c in components) or "- (none)"

    return (
        "Generate the React + Tailwind frontend.\n\n"
        f"## Tech Stack\n{json.dumps(tech_stack, indent=2)}\n\n"
        f"## Components to build (each at src/components/<Name>.jsx)\n"
        f"{components_block}\n\n"
        f"## API endpoints to call (use EXACT method + path)\n"
        f"{endpoints_block}\n\n"
        "Return only the JSON object — no fences, no commentary."
    )


def frontend_agent(state: AgentState) -> AgentState:
    """
    Generate the frontend file map and store it on state["frontend_code"].
    """
    architecture = state["architecture"]

    # format="json" forces Ollama to emit a parseable JSON object instead of
    # free-form text wrapped in commentary.
    llm = get_llm(temperature=0.1).bind(format="json")

    messages = [
        ("system", _load_system_prompt()),
        ("human", _build_human_message(architecture)),
    ]

    response = llm.invoke(messages)
    raw_text = response.content if hasattr(response, "content") else str(response)

    frontend_code = json.loads(raw_text)

    # The LLM regularly emits broken JS for the three build-tool config
    # files (bare object literals, missing wrappers). Their contents are
    # fully predictable, so we overwrite with known-good versions instead
    # of retrying the model.
    _apply_config_fallbacks(frontend_code)

    state["frontend_code"] = frontend_code
    return state
