"""
LangGraph workflow — wires every agent node together and exposes a compiled
`graph` for main.py to invoke.

Flow (per CLAUDE.md):
    START → orchestrator → frontend → backend → ai_agent → reviewer
    reviewer → {pass: file_writer, retry_*: that agent, failed: END}
    file_writer → executor → tester
    tester → {pass: END, fix: fixer → executor, failed: END}
"""

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from agents.ai_agent import ai_agent
from agents.backend_agent import backend_agent
from agents.executor_agent import executor_agent
from agents.fixer_agent import fixer_agent
from agents.frontend_agent import frontend_agent
from agents.orchestrator import orchestrator_agent
from agents.reviewer_agent import reviewer_agent
from agents.tester_agent import tester_agent
from core.file_writer import file_writer
from core.state import AgentState


# Retry caps — keep these aligned with CLAUDE.md.
MAX_REVIEW_RETRIES = 3
MAX_FIX_ATTEMPTS = 3


def decide_next(state: AgentState) -> str:
    """
    Router after the reviewer node.

    Order matters: enforce the retry cap first, then check pass/fail, then
    inspect review_errors for a FRONTEND/BACKEND/AI tag to pick which agent
    to re-run.
    """
    if state.get("retry_count", 0) >= MAX_REVIEW_RETRIES:
        return "failed"

    if state.get("review_passed", False):
        return "pass"

    # Reviewer is expected to tag each error with FRONTEND / BACKEND / AI so
    # we know which agent to re-run. Earliest matching tag wins.
    error_blob = " ".join(str(e) for e in state.get("review_errors", [])).upper()
    if "FRONTEND" in error_blob:
        return "retry_frontend"
    if "BACKEND" in error_blob:
        return "retry_backend"
    if "AI" in error_blob:
        return "retry_ai"

    # Review failed but no tag we can route on — bail out rather than loop.
    return "failed"


def decide_after_test(state: AgentState) -> str:
    """
    Router after the tester node.

    fix_attempts cap is independent of retry_count (the review-loop counter).
    """
    if state.get("fix_attempts", 0) >= MAX_FIX_ATTEMPTS:
        return "failed"

    test_results = state.get("test_results", {})
    if test_results.get("failed", 0) == 0:
        return "pass"
    return "fix"


workflow = StateGraph(AgentState)

workflow.add_node("orchestrator", orchestrator_agent)
workflow.add_node("frontend", frontend_agent)
workflow.add_node("backend", backend_agent)
workflow.add_node("ai_agent", ai_agent)
workflow.add_node("reviewer", reviewer_agent)
workflow.add_node("file_writer", file_writer)
workflow.add_node("executor", executor_agent)
workflow.add_node("tester", tester_agent)
workflow.add_node("fixer", fixer_agent)

# Sequential codegen path.
workflow.add_edge(START, "orchestrator")
workflow.add_edge("orchestrator", "frontend")
workflow.add_edge("frontend", "backend")
workflow.add_edge("backend", "ai_agent")
workflow.add_edge("ai_agent", "reviewer")

# Reviewer branches.
workflow.add_conditional_edges(
    "reviewer",
    decide_next,
    {
        "pass": "file_writer",
        "retry_frontend": "frontend",
        "retry_backend": "backend",
        "retry_ai": "ai_agent",
        "failed": END,
    },
)

# Post-review pipeline.
workflow.add_edge("file_writer", "executor")
workflow.add_edge("executor", "tester")

# Tester branches; fixer always loops back through executor.
workflow.add_conditional_edges(
    "tester",
    decide_after_test,
    {
        "pass": END,
        "fix": "fixer",
        "failed": END,
    },
)
workflow.add_edge("fixer", "executor")


# Persistent checkpointer so a partially-run flow can resume across processes.
# check_same_thread=False lets the connection be shared with LangGraph's
# internal threads.
_conn = sqlite3.connect("agent_memory.db", check_same_thread=False)
checkpointer = SqliteSaver(_conn)

graph = workflow.compile(checkpointer=checkpointer)
