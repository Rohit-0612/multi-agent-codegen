"""
Tester agent — generates and runs happy-path API tests against the
running backend produced by executor_agent.

Two phases:
  1. The LLM proposes test cases from state["architecture"]["api_endpoints"].
     Structured output is enforced with a Pydantic schema so the agent
     never has to clean up free-form text.
  2. Each test case is executed with stdlib urllib.request — no new deps.
     Results are aggregated into state["test_results"].

The agent NEVER raises. Any LLM failure, network error, or malformed
response is captured into the results dict so the downstream fixer can
react instead of the graph blowing up.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from core.llm_factory import get_llm
from core.state import AgentState


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = PROJECT_ROOT / "prompts" / "tester.md"

# Single-request timeout. Tests hit a local backend, so anything over a
# couple of seconds means the endpoint is hung — fail fast and move on.
_HTTP_TIMEOUT = 5.0

# Fallback when the LLM can't be coerced into a valid test plan. The
# executor agent guarantees /api/health exists, so this is always safe.
_FALLBACK_TESTS: List[Dict[str, Any]] = [
    {
        "name": "health check",
        "method": "GET",
        "url": "http://localhost:8000/api/health",
        "payload": None,
        "expected_status": 200,
    }
]


class TestCase(BaseModel):
    """One HTTP test the agent will execute against the live backend."""

    name: str = Field(description="Short lowercase description of the test")
    method: Literal["GET", "POST", "PUT", "DELETE"] = Field(
        description="HTTP method to send"
    )
    url: str = Field(description="Full URL on http://localhost:8000")
    payload: Optional[Dict[str, Any]] = Field(
        default=None, description="JSON body for POST/PUT; null otherwise"
    )
    expected_status: int = Field(description="HTTP status code expected on success")


class TestPlan(BaseModel):
    """Container so with_structured_output gets a top-level object schema."""

    tests: List[TestCase] = Field(description="The test cases to run")


def _load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _extract_models_source(backend_code: dict) -> str:
    """
    Pull the contents of `models.py` (or any file ending in `models.py`)
    out of the backend file map. The tester LLM uses this to ground
    POST/PUT request bodies in the actual Pydantic schema instead of
    guessing field names from the endpoint description.
    """
    if not backend_code:
        return ""
    for name, contents in backend_code.items():
        if name.lower().endswith("models.py") and isinstance(contents, str):
            return contents
    return ""


def _build_human_message(endpoints: List[dict], models_src: str) -> str:
    """Brief the LLM with the endpoint list and the actual Pydantic schemas."""
    endpoints_block = "\n".join(
        f"- {ep['method']} {ep['path']} — {ep['description']}" for ep in endpoints
    ) or "- (none — only generate the /api/health check)"

    schema_block = (
        f"## Request/response schemas (from backend `models.py`)\n```python\n"
        f"{models_src}\n```\n\n"
        "Use these classes verbatim when building POST/PUT payloads — every "
        "required field MUST be present with a value of the declared type. "
        "Optional fields may be omitted."
        if models_src.strip()
        else "## Request/response schemas\n(no models.py provided — infer from endpoint descriptions)"
    )

    return (
        "Generate HTTP test cases for these endpoints:\n\n"
        f"{endpoints_block}\n\n"
        f"{schema_block}\n\n"
        "Return only the JSON object — no fences, no commentary."
    )


def _generate_test_plan(
    endpoints: List[dict], models_src: str
) -> List[Dict[str, Any]]:
    """
    Ask the LLM for a TestPlan. On any failure (network, parse, schema)
    return the deterministic fallback so the agent can still produce a
    meaningful test_results dict.
    """
    try:
        llm = get_llm(temperature=0.1)
        structured_llm = llm.with_structured_output(TestPlan)
        messages = [
            ("system", _load_system_prompt()),
            ("human", _build_human_message(endpoints, models_src)),
        ]
        plan: TestPlan = structured_llm.invoke(messages)
        if not plan.tests:
            return list(_FALLBACK_TESTS)
        return [t.model_dump() for t in plan.tests]
    except Exception:
        # We swallow every exception class on purpose — Ollama can raise
        # ConnectionError, ValidationError, JSONDecodeError, or
        # something model-specific, and the fallback is correct for all
        # of them.
        return list(_FALLBACK_TESTS)


def _run_one(test: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a single test case and translate the outcome into the result
    schema the fixer agent expects.
    """
    name = test.get("name", "<unnamed>")
    method = (test.get("method") or "GET").upper()
    url = test.get("url", "")
    payload = test.get("payload")
    expected = int(test.get("expected_status", 200))

    data = None
    headers = {}
    if payload is not None and method in ("POST", "PUT"):
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, data=data, method=method, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            actual = resp.status
    except urllib.error.HTTPError as exc:
        # HTTPError is still a response — capture its status so a 404 vs
        # a connection refused are distinguishable downstream.
        return {
            "name": name,
            "passed": exc.code == expected,
            "expected_status": expected,
            "actual_status": exc.code,
            "error": None if exc.code == expected else f"HTTP {exc.code}",
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "name": name,
            "passed": False,
            "expected_status": expected,
            "actual_status": 0,
            "error": str(exc),
        }

    return {
        "name": name,
        "passed": actual == expected,
        "expected_status": expected,
        "actual_status": actual,
        "error": None if actual == expected else f"expected {expected}, got {actual}",
    }


def tester_agent(state: AgentState) -> AgentState:
    """Run LLM-designed API tests; write state['test_results']."""
    execution_result = state.get("execution_result", {}) or {}

    # If the executor never got the backend running there's no point
    # generating tests — short-circuit to the spec'd failure shape.
    if not execution_result.get("backend_running"):
        state["test_results"] = {
            "total": 0,
            "passed": 0,
            "failed": 1,
            "failures": [{"error": "backend not running"}],
        }
        return state

    architecture = state.get("architecture", {}) or {}
    endpoints = architecture.get("api_endpoints", []) or []
    # `models.py` from the backend file map gives the LLM the exact
    # Pydantic schemas to populate. Without this, POST/PUT payloads
    # were guessed from endpoint descriptions and 422'd routinely.
    models_src = _extract_models_source(state.get("backend_code", {}) or {})

    tests = _generate_test_plan(endpoints, models_src)
    results = [_run_one(t) for t in tests]

    passed = sum(1 for r in results if r["passed"])
    failures = [r for r in results if not r["passed"]]

    state["test_results"] = {
        "total": len(results),
        "passed": passed,
        "failed": len(failures),
        "failures": failures,
    }
    return state
