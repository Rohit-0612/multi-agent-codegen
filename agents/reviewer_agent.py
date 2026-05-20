"""
Reviewer agent — audits the combined frontend / backend / ai outputs for
contract mismatches before they get written to disk.

Reads:
  - state["architecture"]   — for the canonical api_endpoints list.
  - state["frontend_code"]  — only filenames + first 50 lines of App.jsx.
  - state["backend_code"]   — only filenames + first 50 lines of main.py.
  - state["ai_code"]        — only filenames.

The LLM does the review (NOT string matching). We summarize aggressively
before invoking it: passing full file contents would blow the context
window for any non-trivial app, and the meaningful contract checks
(endpoint match, component imports, schema sanity) only need the heads of
the two entry-point files plus the filename lists.

Writes:
  - state["review_passed"] : bool
  - state["review_errors"] : list[{"agent": str, "error": str}]
  - state["retry_count"]   : incremented on failure (capped in graph.py).
"""

import json
from pathlib import Path
from typing import List, Literal

from pydantic import BaseModel, Field

from core.llm_factory import get_llm
from core.state import AgentState


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = PROJECT_ROOT / "prompts" / "reviewer.md"

# How many lines of the entry-point files we hand to the LLM. 50 is enough
# to see imports, the FastAPI app construction / first routes, and the top
# of the React component tree — which is where the contract bugs live.
_ENTRY_FILE_LINE_LIMIT = 50


class ReviewError(BaseModel):
    """A single contract mismatch the reviewer found."""

    agent: Literal["frontend", "backend", "ai"] = Field(
        description="Which agent's output is responsible for the issue"
    )
    error: str = Field(description="One-sentence description of the mismatch")


class ReviewResult(BaseModel):
    """Structured verdict returned by the reviewer LLM."""

    passed: bool = Field(description="True if no issues were found")
    errors: List[ReviewError] = Field(
        default_factory=list,
        description="Empty when passed is True; otherwise the list of issues",
    )


def _load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _head(text: str, n: int = _ENTRY_FILE_LINE_LIMIT) -> str:
    """Return the first n lines of text (or all of it, if shorter)."""
    if not isinstance(text, str):
        return ""
    lines = text.splitlines()
    return "\n".join(lines[:n])


def _filenames(code: dict) -> List[str]:
    """Sorted list of filenames in a {filename: contents} dict."""
    if not code:
        return []
    return sorted(code.keys())


def _build_human_message(
    architecture: dict,
    frontend_code: dict,
    backend_code: dict,
    ai_code: dict,
) -> str:
    """Build the compact review payload — endpoints, filenames, two heads."""
    endpoints = architecture.get("api_endpoints", []) or []
    endpoints_block = "\n".join(
        f"- {ep['method']} {ep['path']} — {ep['description']}" for ep in endpoints
    ) or "- (none)"

    frontend_files = _filenames(frontend_code)
    backend_files = _filenames(backend_code)
    ai_files = _filenames(ai_code)

    frontend_files_block = "\n".join(f"- {f}" for f in frontend_files) or "- (none)"
    backend_files_block = "\n".join(f"- {f}" for f in backend_files) or "- (none)"
    ai_files_block = "\n".join(f"- {f}" for f in ai_files) or "- (none)"

    # App.jsx and main.py are the only files whose contents we ship. Look
    # them up case-insensitively so a stray "app.jsx" or "Main.py" still
    # finds the entry point.
    app_jsx = ""
    for name, contents in (frontend_code or {}).items():
        if name.lower().endswith("app.jsx"):
            app_jsx = contents if isinstance(contents, str) else ""
            break

    main_py = ""
    for name, contents in (backend_code or {}).items():
        if name.lower().endswith("main.py"):
            main_py = contents if isinstance(contents, str) else ""
            break

    return (
        "Review the generated app for contract mismatches.\n\n"
        f"## API Endpoints (from architecture)\n{endpoints_block}\n\n"
        f"## Frontend files\n{frontend_files_block}\n\n"
        f"## Backend files\n{backend_files_block}\n\n"
        f"## AI files\n{ai_files_block}\n\n"
        f"## First {_ENTRY_FILE_LINE_LIMIT} lines of frontend App.jsx\n"
        "```\n"
        f"{_head(app_jsx)}\n"
        "```\n\n"
        f"## First {_ENTRY_FILE_LINE_LIMIT} lines of backend main.py\n"
        "```\n"
        f"{_head(main_py)}\n"
        "```\n\n"
        "Return only the JSON object — no fences, no commentary."
    )


def reviewer_agent(state: AgentState) -> AgentState:
    """Run the LLM review and update state with the verdict."""
    architecture = state["architecture"]
    frontend_code = state.get("frontend_code", {}) or {}
    backend_code = state.get("backend_code", {}) or {}
    ai_code = state.get("ai_code", {}) or {}

    # with_structured_output binds the Pydantic schema so the model returns
    # a parsed ReviewResult instead of free-form text we'd have to clean up.
    llm = get_llm(temperature=0.1)
    structured_llm = llm.with_structured_output(ReviewResult)

    messages = [
        ("system", _load_system_prompt()),
        (
            "human",
            _build_human_message(architecture, frontend_code, backend_code, ai_code),
        ),
    ]

    result: ReviewResult = structured_llm.invoke(messages)

    if result.passed:
        state["review_passed"] = True
        state["review_errors"] = []
    else:
        state["review_passed"] = False
        state["review_errors"] = [err.model_dump() for err in result.errors]
        # retry_count gates the graph's retry loop — start at 0 if missing.
        state["retry_count"] = state.get("retry_count", 0) + 1

    return state
