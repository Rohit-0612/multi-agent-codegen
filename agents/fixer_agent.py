"""
Fixer agent — single-file LLM repair driven by the first failing test.

The tester agent produced a list of failures; we look at the first one,
map its actual_status to the most likely culprit file in backend_code,
and ask the LLM to return the full fixed contents of that one file. The
fix is written to disk so the next executor/tester loop sees it, and
mirrored back into state["backend_code"] so the state stays consistent
with the filesystem.

Reads:
  - state["test_results"]["failures"]
  - state["backend_code"]
  - state["frontend_code"]    — present so future iterations can fix it;
                                currently only inspected, not edited.
  - state["architecture"]
  - state["output_dir"]
  - state["fix_attempts"]

Target-file selection:
  - If <output_dir>/backend.log contains a Python traceback, the deepest
    File "..." frame whose basename is in backend_code wins. For import
    errors (SyntaxError, ImportError) this points at the actual broken
    file, which the actual_status alone can't reveal.
  - Otherwise fall back to the actual_status → file mapping in
    _STATUS_TO_FILE.

Writes:
  - state["backend_code"][<filename>] — replaced with the LLM's output.
  - state["fix_attempts"]             — incremented by exactly 1 each call.
  - <output_dir>/backend/<filename>   — overwritten on disk.

The agent NEVER raises. Empty LLM output is treated as "nothing to apply"
— we still increment fix_attempts so graph.py's retry cap can break the
loop instead of spinning forever.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from agents.backend_agent import harden_backend_code
from core.llm_factory import get_llm
from core.state import AgentState


class FixedFile(BaseModel):
    """Structured LLM output: just the complete fixed file as a string."""

    code: str = Field(
        description="Complete fixed file contents. Plain source code, no markdown fences."
    )


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = PROJECT_ROOT / "prompts" / "fixer.md"

# actual_status → most likely culprit file. main.py is the catch-all
# because route registration, startup hooks, and request handlers all
# live there in the generated layout.
_STATUS_TO_FILE: Dict[int, str] = {
    0: "main.py",     # connection refused → backend didn't start
    404: "main.py",   # missing route
    422: "models.py", # Pydantic rejected the payload
    500: "main.py",   # handler raised
}
_DEFAULT_TARGET_FILE = "main.py"

# How many lines of backend.log to expose to the LLM. Matches the
# executor's own tail size — the traceback is always at the end of the
# log, so 50 lines is plenty.
_LOG_TAIL_LINES = 50

# Matches each frame in a Python traceback: `File "<path>", line N`.
# Captures the path. The path may be a real file or a synthetic marker
# like `<frozen importlib._bootstrap>`; the caller filters by whether
# the basename is present in backend_code.
_TRACEBACK_FILE_RE = re.compile(r'File "([^"]+)", line \d+')

# Leading / trailing markdown code fences. Pydantic structured output is
# the primary defense — these regexes are a belt-and-suspenders strip
# for when the model includes fences inside the JSON string anyway.
_LEADING_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n")
_TRAILING_FENCE_RE = re.compile(r"\n?\s*```\s*$")


def _strip_fences(text: str) -> str:
    """Remove leading ```python / ```py / ``` and trailing ``` if present."""
    text = text.strip()
    text = _LEADING_FENCE_RE.sub("", text)
    text = _TRAILING_FENCE_RE.sub("", text)
    return text.strip()


def _load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _read_backend_log_tail(output_dir: Optional[str], n: int = _LOG_TAIL_LINES) -> str:
    """Return the tail of <output_dir>/backend.log, or empty string."""
    if not output_dir:
        return ""
    log_path = Path(output_dir) / "backend.log"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _pick_file_from_log(log_text: str, backend_code: Dict[str, str]) -> Optional[str]:
    """
    Scan a backend.log excerpt for Python-traceback `File "..."` lines and
    return the deepest one whose basename appears in backend_code. Returns
    None when no traceback frame points at a generated file.

    Picking the LAST match is intentional: in a Python traceback the
    deepest frame is the actual error site, and for SyntaxError the
    traceback ends with the broken file itself.
    """
    if not log_text or not backend_code:
        return None

    chosen: Optional[str] = None
    for match in _TRACEBACK_FILE_RE.finditer(log_text):
        name = Path(match.group(1)).name
        if name in backend_code:
            chosen = name
    return chosen


def _pick_target_file(failure: Dict[str, Any], backend_code: Dict[str, str]) -> str:
    """
    Decide which file to hand to the LLM. Falls back to main.py if the
    mapped file doesn't actually exist in the generated backend (e.g.
    LLM emitted everything inline without a separate models.py).
    """
    status = failure.get("actual_status")
    candidate = _STATUS_TO_FILE.get(status, _DEFAULT_TARGET_FILE)

    if candidate in backend_code:
        return candidate

    # Mapped file wasn't generated — main.py is always present.
    if _DEFAULT_TARGET_FILE in backend_code:
        return _DEFAULT_TARGET_FILE

    # Truly degenerate state: no main.py either. Return the first file
    # so the LLM at least has something to work with.
    return next(iter(backend_code))


def _build_human_message(
    failure: Dict[str, Any],
    filename: str,
    file_contents: str,
    endpoints: list,
    log_tail: str = "",
) -> str:
    """Compose the human message: failure + (optional log) + endpoints + the file."""
    endpoints_block = "\n".join(
        f"- {ep['method']} {ep['path']} — {ep['description']}" for ep in endpoints
    ) or "- (none)"

    # Log block goes right after the failing test so the LLM sees the
    # diagnostic info together. Omitted entirely when no log is
    # available — we don't want to confuse the model with an empty fence.
    log_block = ""
    if log_tail:
        log_block = (
            "## Backend server log (last 50 lines)\n"
            "Use this to identify the exact error location and type.\n"
            "```\n"
            f"{log_tail}\n"
            "```\n\n"
        )

    return (
        "Fix the file below so the failing test passes.\n\n"
        "## Failing test\n"
        f"{json.dumps(failure, indent=2)}\n\n"
        f"{log_block}"
        "## Architecture — api_endpoints\n"
        f"{endpoints_block}\n\n"
        f"## Current contents of {filename}\n"
        "```\n"
        f"{file_contents}\n"
        "```\n\n"
        "Return ONLY the complete fixed contents of the file — no fences, "
        "no commentary."
    )


def _ask_llm_for_fix(
    failure: Dict[str, Any],
    filename: str,
    file_contents: str,
    endpoints: list,
    log_tail: str = "",
) -> Optional[str]:
    """
    Invoke the LLM via structured output and return the fixed file
    contents. Returns None on any failure so the caller can skip the
    write without crashing.

    Two layers of format defense:
      1. with_structured_output(FixedFile) — Pydantic enforces a JSON
         object with a single `code` string field, so we never have to
         parse free-form text.
      2. _strip_fences — belt-and-suspenders against the model wrapping
         its own string value in ```python ... ``` anyway. Models trained
         on stack-overflow-style answers do this even when told not to.
    """
    try:
        llm = get_llm(temperature=0.1)
        structured_llm = llm.with_structured_output(FixedFile)
        messages = [
            ("system", _load_system_prompt()),
            (
                "human",
                _build_human_message(
                    failure, filename, file_contents, endpoints, log_tail
                ),
            ),
        ]
        result: FixedFile = structured_llm.invoke(messages)
    except Exception:
        # Network / Ollama / Pydantic-validation failures all funnel here;
        # the graph treats no-op fixes as exhausting an attempt.
        return None

    if not isinstance(result, FixedFile) or not isinstance(result.code, str):
        return None
    return _strip_fences(result.code)


def fixer_agent(state: AgentState) -> AgentState:
    """Apply a single targeted fix to one backend file."""
    # Always count this call as an attempt, even if we bail out below —
    # graph.py uses fix_attempts to break the retry loop.
    state["fix_attempts"] = state.get("fix_attempts", 0) + 1

    test_results = state.get("test_results", {}) or {}
    failures = test_results.get("failures", []) or []
    backend_code = state.get("backend_code", {}) or {}

    # Nothing to fix (or no backend to fix into) — leave state alone.
    if not failures or not backend_code:
        return state

    failure = failures[0]
    output_dir = state.get("output_dir")

    # Prefer the traceback in backend.log over the status-code guess —
    # for import-time failures (SyntaxError, ImportError) the status is
    # always 0 and the real broken file is named in the traceback (often
    # database.py / models.py, not main.py).
    log_tail = _read_backend_log_tail(output_dir)
    target_file = _pick_file_from_log(log_tail, backend_code)
    if target_file is None:
        target_file = _pick_target_file(failure, backend_code)

    current_contents = backend_code.get(target_file, "")
    endpoints = (state.get("architecture", {}) or {}).get("api_endpoints", []) or []

    fixed = _ask_llm_for_fix(
        failure, target_file, current_contents, endpoints, log_tail=log_tail
    )
    if not fixed or not fixed.strip():
        # Empty / failed response — the attempt is spent, but we don't
        # corrupt the file on disk with garbage.
        return state

    # Mirror the fix into state first; the in-memory dict is the source
    # of truth for any downstream agent that re-runs in the same flow.
    backend_code[target_file] = fixed

    # Run the same hardening chain backend_agent uses, so the fixer's
    # LLM regen goes through the deterministic patches (Optional /
    # status_code / Response import) and the targeted checks
    # (row_factory, @contextmanager, import probe). Without this, the
    # fixer regularly re-introduces the very bugs those checks were
    # built to catch — that's why fix-loop runs would plateau at 3/5.
    harden_backend_code(backend_code)
    state["backend_code"] = backend_code

    # Persist EVERY backend .py file to disk (not just target_file) —
    # harden_backend_code may have rewritten neighbors too, e.g. a
    # row_factory fix in database.py while we were nominally fixing
    # main.py. The executor agent picks up whatever is on disk next.
    output_dir = state.get("output_dir")
    if output_dir:
        backend_dir = Path(output_dir) / "backend"
        try:
            backend_dir.mkdir(parents=True, exist_ok=True)
            for fname, contents in backend_code.items():
                if not isinstance(contents, str):
                    continue
                (backend_dir / fname).parent.mkdir(parents=True, exist_ok=True)
                (backend_dir / fname).write_text(contents, encoding="utf-8")
        except OSError:
            # Filesystem errors shouldn't kill the graph; the state copy
            # is still updated for any in-process consumers.
            pass

    return state
