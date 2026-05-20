"""
AgentState — shared state passed between all LangGraph nodes.

Every agent reads from and writes to this dict. total=False is mandatory:
nodes only populate the fields they own, and downstream nodes must
tolerate missing keys (e.g. fixer fields are absent until the fixer runs).
"""

from typing import TypedDict


class AgentState(TypedDict, total=False):
    # Raw user request — set once by main.py, read by orchestrator.
    user_prompt: str

    # Orchestrator's plan: tech stack breakdown, file list, component layout.
    # Consumed by every downstream codegen agent.
    architecture: dict

    # Frontend agent output: {filename: file_contents} for the React app.
    frontend_code: dict

    # Backend agent output: {filename: file_contents} for the FastAPI app.
    backend_code: dict

    # AI agent output: {filename: file_contents} for the LangChain layer.
    ai_code: dict

    # Config/glue files: docker-compose.yml, Dockerfiles, requirements.txt, etc.
    config_code: dict

    # Reviewer verdict. True = all generated code passed review.
    review_passed: bool

    # Reviewer's list of issues found, used to route retries and brief the
    # agent being re-run.
    review_errors: list

    # How many times we've looped back through the codegen agents after a
    # failed review. Capped in graph.py to prevent infinite retries.
    retry_count: int

    # Filesystem path where file_writer dumped the generated project.
    output_dir: str

    # Path to the zipped artifact handed to the user at the end.
    zip_path: str

    # Executor agent output: docker-compose up result, container status,
    # stdout/stderr, exposed ports.
    execution_result: dict

    # Tester agent output: which tests ran, pass/fail counts, failure logs.
    test_results: dict

    # How many times the fixer has tried to repair failing tests. Capped in
    # graph.py — separate from retry_count (which is for review failures).
    fix_attempts: int

    # Terminal status string written by the last node before END
    # (e.g. "success", "review_failed", "tests_failed").
    final_status: str
