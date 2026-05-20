"""
Entry point for the multi-agent code generation system.

Reads a prompt (CLI arg or interactive input), invokes the compiled
LangGraph workflow streaming-style so per-phase progress can be reported,
then prints a summary box and tears down any dev servers the executor
agent left running.

The compiled `graph` from core/graph.py already wires in a SqliteSaver
checkpointer against agent_memory.db, so main.py only owns the thread_id
and the user-facing reporting.
"""

import sys
import uuid

from agents.executor_agent import cleanup
from core.graph import graph


# Map every graph node to the user-facing phase banner. Multiple nodes
# share a phase (codegen = 3 agents, run/test = 3 agents); we de-dup
# below so each banner prints at most once per run.
_NODE_TO_PHASE = {
    "orchestrator": "[1/5] Planning architecture...",
    "frontend":     "[2/5] Generating code...",
    "backend":      "[2/5] Generating code...",
    "ai_agent":     "[2/5] Generating code...",
    "reviewer":     "[3/5] Reviewing code...",
    "file_writer":  "[4/5] Writing files...",
    "executor":     "[5/5] Running and testing...",
    "tester":       "[5/5] Running and testing...",
    "fixer":        "[5/5] Running and testing...",
}


def _get_prompt() -> str:
    """CLI arg wins; otherwise prompt the user interactively."""
    if len(sys.argv) > 1 and any(arg.strip() for arg in sys.argv[1:]):
        return " ".join(sys.argv[1:]).strip()
    try:
        return input("What should I build? > ").strip()
    except EOFError:
        return ""


def _derive_final_status(state: dict) -> str:
    """
    Compute a terminal status string from the final state. No agent
    currently writes final_status, so this is the canonical source —
    but if some future agent sets it, we honor that.
    """
    if state.get("final_status"):
        return state["final_status"]

    if state.get("review_passed") is False:
        return "review_failed"

    execution_result = state.get("execution_result") or {}
    if not execution_result:
        return "execution_skipped"
    if not execution_result.get("success"):
        return "execution_failed"

    test_results = state.get("test_results") or {}
    if test_results.get("failed", 0) > 0:
        return "tests_failed"
    if test_results.get("total", 0) == 0:
        return "no_tests_run"

    return "success"


def _print_summary(state: dict, status: str) -> None:
    """Render the final box shown to the user."""
    test_results = state.get("test_results") or {}
    total = test_results.get("total", 0)
    passed = test_results.get("passed", 0)
    tests_line = f"{passed}/{total} passed" if total else "no tests run"

    bar = "=" * 32
    print()
    print(bar)
    print("MULTI-AGENT CODEGEN — COMPLETE")
    print(bar)
    print(f"Status      : {status}")
    print(f"Output dir  : {state.get('output_dir', '(none)')}")
    print(f"ZIP         : {state.get('zip_path', '(none)')}")
    print(f"Tests       : {tests_line}")
    print(bar)


def main() -> None:
    user_prompt = _get_prompt()
    if not user_prompt:
        print("No prompt provided. Exiting.")
        return

    initial_state = {
        "user_prompt": user_prompt,
        "retry_count": 0,
        "fix_attempts": 0,
    }

    # Fresh thread per run — the SqliteSaver in core/graph.py keys
    # checkpoints by thread_id, and a unique id keeps unrelated runs from
    # resuming each other's state.
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    print(f"\nThread: {thread_id}\n")

    # We stream (not invoke) so we can emit a phase banner each time a
    # new node executes. The updates-mode chunks are {node_name: delta};
    # we keep a local mirror of state so the summary box has the final
    # values without a separate get_state() round-trip.
    final_state: dict = dict(initial_state)
    seen_phases: set = set()

    try:
        for chunk in graph.stream(
            initial_state, config=config, stream_mode="updates"
        ):
            for node, update in chunk.items():
                phase = _NODE_TO_PHASE.get(node)
                if phase and phase not in seen_phases:
                    print(phase)
                    seen_phases.add(phase)
                if isinstance(update, dict):
                    final_state.update(update)
    except KeyboardInterrupt:
        print("\nInterrupted. Cleaning up background processes...")
    finally:
        # Always tear down the executor's dev servers — otherwise ports
        # 8000/3000 stay occupied past process exit on some shells.
        cleanup()

    status = _derive_final_status(final_state)
    _print_summary(final_state, status)


if __name__ == "__main__":
    main()
