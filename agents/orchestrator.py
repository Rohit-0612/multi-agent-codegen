"""
Orchestrator agent — first node in the LangGraph flow.

Reads state["user_prompt"] and produces state["architecture"] — the structured
plan every downstream codegen agent (frontend, backend, ai) depends on.
Also writes a human-readable architecture.md at the project root for review.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from core.llm_factory import get_llm
from core.state import AgentState


# Project root = parent of the agents/ directory this file lives in.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = PROJECT_ROOT / "prompts" / "orchestrator.md"
ARCHITECTURE_MD_PATH = PROJECT_ROOT / "architecture.md"


class APIEndpoint(BaseModel):
    """One FastAPI route the backend agent must implement."""

    method: str = Field(description="HTTP method: GET, POST, PUT, or DELETE")
    path: str = Field(description="URL path, must start with /api/")
    description: str = Field(description="One-sentence description of the endpoint")


class Architecture(BaseModel):
    """Structured architecture plan returned by the orchestrator LLM."""

    project_name: str = Field(description="Filesystem-safe snake_case project name")
    tech_stack: Dict[str, str] = Field(
        description="Layer -> tech mapping, e.g. {'frontend': 'React'}"
    )
    api_endpoints: List[APIEndpoint] = Field(
        description="REST endpoints the backend must expose"
    )
    frontend_components: List[str] = Field(
        description="PascalCase React component names"
    )
    folder_structure: Dict[str, Any] = Field(
        description="Nested dict mirroring the project directory tree"
    )


def _load_system_prompt() -> str:
    """Read the orchestrator system prompt at call time so edits don't need a reimport."""
    return PROMPT_PATH.read_text(encoding="utf-8")


def _render_architecture_markdown(arch: dict) -> str:
    """Render the architecture dict as readable Markdown for the on-disk artifact."""
    lines: List[str] = [f"# {arch.get('project_name', 'Project')}", ""]

    lines.append("## Tech Stack")
    for layer, tech in arch.get("tech_stack", {}).items():
        lines.append(f"- **{layer}**: {tech}")
    lines.append("")

    lines.append("## API Endpoints")
    for ep in arch.get("api_endpoints", []):
        lines.append(f"- `{ep['method']} {ep['path']}` — {ep['description']}")
    lines.append("")

    lines.append("## Frontend Components")
    for component in arch.get("frontend_components", []):
        lines.append(f"- {component}")
    lines.append("")

    lines.append("## Folder Structure")
    lines.append("```json")
    lines.append(json.dumps(arch.get("folder_structure", {}), indent=2))
    lines.append("```")

    return "\n".join(lines) + "\n"


def orchestrator_agent(state: AgentState) -> AgentState:
    """
    Analyze the user prompt and populate state["architecture"].

    Side effect: writes architecture.md at the project root.
    """
    user_prompt = state["user_prompt"]

    # with_structured_output binds the Pydantic schema so the model returns
    # a parsed Architecture object instead of free-form text.
    llm = get_llm(temperature=0.1)
    structured_llm = llm.with_structured_output(Architecture)

    messages = [
        ("system", _load_system_prompt()),
        ("human", user_prompt),
    ]

    architecture: Architecture = structured_llm.invoke(messages)
    arch_dict = architecture.model_dump()

    # Human-readable artifact for debugging / review.
    ARCHITECTURE_MD_PATH.write_text(
        _render_architecture_markdown(arch_dict), encoding="utf-8"
    )

    state["architecture"] = arch_dict
    return state
