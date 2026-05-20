"""
Backend agent — generates FastAPI + SQLite code from the architecture plan
and the frontend code that will call into it.

Reads:
  - state["architecture"]["api_endpoints"]
  - state["architecture"]["tech_stack"]
  - state["frontend_code"] — scanned for the /api/... paths it actually calls,
    so we can flag any mismatch with the architecture for the LLM.

Writes state["backend_code"] as a {filename: file_contents} dict.
"""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.llm_factory import get_llm
from core.state import AgentState


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = PROJECT_ROOT / "prompts" / "backend.md"

# Matches /api/<segments> in source code. Stops at quotes/whitespace/punctuation
# that wouldn't appear inside a URL path. Allows {placeholders} like {id}.
_API_PATH_RE = re.compile(r"/api/[A-Za-z0-9_\-/{}]+")

# Deterministic fallback for requirements.txt. Contents are fully
# predictable from the backend prompt, but the LLM forgets to emit the
# file in ~1 of every few runs — synthesize it locally instead of retrying.
_DEFAULT_REQUIREMENTS = (
    "fastapi\n"
    "uvicorn[standard]\n"
    "pydantic\n"
    "python-dotenv\n"
)

# Matches `: str = None` / `: int = None` without `Optional[...]`. Pydantic v2
# rejects these — the field type is non-Optional but the default is None,
# so any response that omits the field 500s during serialization.
_BARE_OPTIONAL_STR_RE = re.compile(r":\s*str\s*=\s*None\b")
_BARE_OPTIONAL_INT_RE = re.compile(r":\s*int\s*=\s*None\b")

# Matches a FastAPI route decorator like `@app.post("/foo", response_model=Bar)`.
# Group 1 is the decorator + path, group 2 is whatever else is inside the
# parens (may be empty), group 3 is the closing paren. We use this to
# splice `status_code=N` into POST/DELETE decorators that omit it.
_ROUTE_DECORATOR_RE_FMT = r'(@app\.{verb}\(\s*"[^"]*")([^)]*)(\))'

# How many times we'll ask the LLM to repair a single .py file before
# giving up and reverting to the original. Two is enough — past that the
# model is either stuck in a loop or the file is too far gone.
_REGEN_MAX_ATTEMPTS = 2

# Matches `def name(... call_next ...)` only when NOT already preceded by
# `async `. Used to promote middleware-shaped functions whose body uses
# `await call_next` to `async def`. Bounded by `[^)]*` so the regex can't
# accidentally swallow two function signatures in one match.
_DEF_WITH_CALL_NEXT_RE = re.compile(
    r"(?<!async )\bdef (\w+\([^)]*call_next[^)]*\))"
)

# Cap on how long the import probe is allowed to run. The probe does
# nothing but `import main` in a subprocess — anything past a couple
# seconds means a module-level loop or a blocking I/O call, both of
# which the executor agent would also fail on.
_IMPORT_PROBE_TIMEOUT = 10

# Matches `File "path/to/x.py", line N` frames in a Python traceback.
# Used to point the LLM at the specific file that raised at import
# time. Paths never contain `"` so the character class is safe.
_TRACEBACK_FILE_RE = re.compile(r'File "([^"]+\.py)", line \d+')


def _ensure_optional_import(content: str) -> str:
    """
    Make sure `Optional` resolves to `typing.Optional` in `content`.

    Steps:
      1. Scrub `Optional` out of any `from X import ...` line where X is
         NOT `typing` — the LLM regularly tries `from pydantic import
         Optional` or `from fastapi import Optional`, which fails at
         import time.
      2. If `from typing import ...` exists, append `Optional` if it's
         not already in the list.
      3. Otherwise insert a new `from typing import Optional` line right
         after the last existing import.

    Idempotent. Preserves the trailing newline of the input.
    """
    lines = content.splitlines()
    had_trailing_nl = content.endswith("\n")

    # --- Step 1: scrub bad Optional imports --------------------------
    scrubbed: List[str] = []
    for line in lines:
        m = re.match(r"^from (\S+) import (.+)$", line)
        if not m or m.group(1) == "typing":
            scrubbed.append(line)
            continue
        names = [n.strip() for n in m.group(2).split(",")]
        names = [n for n in names if n and n != "Optional"]
        if names:
            scrubbed.append(f"from {m.group(1)} import {', '.join(names)}")
        # If the only name was Optional, drop the line entirely.

    # --- Step 2/3: ensure typing.Optional is imported ----------------
    typing_idx = -1
    for i, line in enumerate(scrubbed):
        if line.startswith("from typing import"):
            typing_idx = i
            break

    if typing_idx >= 0:
        m = re.match(r"^from typing import (.+)$", scrubbed[typing_idx])
        names = [n.strip() for n in m.group(1).split(",")] if m else []
        if "Optional" not in names:
            names.append("Optional")
            scrubbed[typing_idx] = f"from typing import {', '.join(names)}"
    else:
        last_import = -1
        for i, line in enumerate(scrubbed):
            if line.startswith("from ") or line.startswith("import "):
                last_import = i
        insert_at = last_import + 1 if last_import >= 0 else 0
        scrubbed.insert(insert_at, "from typing import Optional")

    result = "\n".join(scrubbed)
    return result + ("\n" if had_trailing_nl else "")


def _patch_optional_fields(content: str) -> str:
    """
    Rewrite bare `: str = None` / `: int = None` to `Optional[...]`, and
    repair the `Optional` import on any file that already references
    `Optional[...]` (regen steps in this agent regularly drop or
    misroute the import after a fix-up round).
    """
    has_bare = bool(
        _BARE_OPTIONAL_STR_RE.search(content) or _BARE_OPTIONAL_INT_RE.search(content)
    )
    if not has_bare and "Optional[" not in content:
        return content
    content = _ensure_optional_import(content)
    if has_bare:
        content = _BARE_OPTIONAL_STR_RE.sub(": Optional[str] = None", content)
        content = _BARE_OPTIONAL_INT_RE.sub(": Optional[int] = None", content)
    return content


def _patch_route_status(content: str, verb: str, status: int) -> str:
    """
    Add `status_code=N` to every `@app.<verb>(...)` decorator that doesn't
    already declare one. Path stays untouched; the new arg is spliced in
    right after the path literal.
    """
    pattern = re.compile(_ROUTE_DECORATOR_RE_FMT.format(verb=verb))

    def _insert(match: "re.Match[str]") -> str:
        head, args, close = match.group(1), match.group(2), match.group(3)
        if "status_code" in args:
            return match.group(0)
        return f"{head}, status_code={status}{args}{close}"

    return pattern.sub(_insert, content)


def _apply_backend_post_processing(backend_code: dict) -> None:
    """
    Deterministic safety net for bugs the LLM ships repeatedly:
      1. Bare `field: str = None` / `field: int = None` — rewritten to
         `Optional[...]` (plus a typing import) in every .py file that
         has the pattern.
      2. `@app.post("/x")` missing `status_code=201` — spliced in.
      3. `@app.delete("/x")` missing `status_code=204` — spliced in.

    Mutates `backend_code` in place.
    """
    for filename, contents in list(backend_code.items()):
        if not isinstance(contents, str) or not filename.endswith(".py"):
            continue

        patched = _patch_optional_fields(contents)
        if filename.endswith("main.py"):
            patched = _patch_route_status(patched, "post", 201)
            patched = _patch_route_status(patched, "delete", 204)

        if patched != contents:
            backend_code[filename] = patched


def _load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _extract_frontend_api_paths(frontend_code: dict) -> List[str]:
    """Scan frontend file contents for /api/... paths the UI tries to hit."""
    if not frontend_code:
        return []

    seen = set()
    for contents in frontend_code.values():
        if not isinstance(contents, str):
            continue
        seen.update(_API_PATH_RE.findall(contents))

    return sorted(seen)


def _build_human_message(architecture: dict, frontend_code: dict) -> str:
    """Hand the LLM the contract: arch endpoints + paths the frontend uses."""
    endpoints = architecture.get("api_endpoints", [])
    tech_stack = architecture.get("tech_stack", {})

    endpoints_block = "\n".join(
        f"- {ep['method']} {ep['path']} — {ep['description']}" for ep in endpoints
    ) or "- (none)"

    frontend_paths = _extract_frontend_api_paths(frontend_code)
    frontend_block = (
        "\n".join(f"- {p}" for p in frontend_paths)
        if frontend_paths
        else "- (no /api paths detected in frontend code)"
    )

    return (
        "Generate the FastAPI + SQLite backend.\n\n"
        f"## Tech Stack\n{json.dumps(tech_stack, indent=2)}\n\n"
        f"## Required endpoints (from architecture — implement ALL of these)\n"
        f"{endpoints_block}\n\n"
        f"## Frontend is calling these paths (each must be implemented)\n"
        f"{frontend_block}\n\n"
        "Return only the JSON object — no fences, no commentary."
    )


def _regenerate_python_file(
    filename: str, broken_code: str, error_msg: str
) -> Optional[str]:
    """
    Ask the LLM to rewrite a single .py file that failed to parse. Returns
    the corrected source on success or None on any failure (network,
    invalid JSON, empty string, etc.) — the caller treats None as "give
    up and keep what we had".

    We use `format="json"` to force a structured response, ask for a
    single-key object keyed by the filename, and fall back to any string
    value in the response in case the model relabels the key.
    """
    try:
        llm = get_llm(temperature=0.1).bind(format="json")
        prompt = (
            f"This code has a SyntaxError: {error_msg}\n"
            "Fix it and return the complete corrected file.\n\n"
            f"Return ONLY a JSON object with a single key {json.dumps(filename)} "
            "whose value is the full corrected file contents as a string. "
            "No fences, no commentary.\n\n"
            "Broken code:\n"
            f"{broken_code}"
        )
        response = llm.invoke([("human", prompt)])
        raw = response.content if hasattr(response, "content") else str(response)
        parsed = json.loads(raw)
        fixed = parsed.get(filename)
        if isinstance(fixed, str) and fixed.strip():
            return fixed
        # Some Ollama models relabel the key — accept any string value as
        # long as exactly one is present, otherwise we can't tell which is
        # the file.
        string_values = [v for v in parsed.values() if isinstance(v, str) and v.strip()]
        if len(string_values) == 1:
            return string_values[0]
    except Exception as exc:  # noqa: BLE001 — defensive: every failure path collapses to None
        print(f"[backend_agent] regen for {filename} failed: {exc}")
    return None


def _fix_async_middleware(content: str) -> str:
    """
    Promote `def name(...call_next...)` to `async def name(...call_next...)`
    when the file uses `await call_next`. The negative lookbehind on
    `async ` keeps already-async functions untouched, so this is safe to
    run on every parseable file.
    """
    if "await call_next" not in content:
        return content
    return _DEF_WITH_CALL_NEXT_RE.sub(r"async def \1", content)


def _validate_and_repair_python_files(backend_code: dict) -> None:
    """
    Last line of defense before the file_writer dumps the file map to
    disk. For every .py file:

      1. Try `ast.parse`. If it parses, no LLM round-trip.
      2. On SyntaxError, ask the LLM to rewrite the file with the error
         message in hand. Up to `_REGEN_MAX_ATTEMPTS` attempts per file.
      3. If still unparseable after the budget, revert to the original
         contents — a broken-but-original file at least gives the user
         something coherent to inspect, vs. a half-regenerated file.
      4. Once parseable, apply `_fix_async_middleware` so a
         `def middleware(req, call_next): await call_next(...)` slips
         through to `async def`.

    Mutates `backend_code` in place. Logs progress to stdout so a CLI run
    surfaces what was repaired.
    """
    for filename, original in list(backend_code.items()):
        if not isinstance(original, str) or not filename.endswith(".py"):
            continue

        current = original
        attempts = 0
        while attempts < _REGEN_MAX_ATTEMPTS:
            try:
                ast.parse(current)
                break
            except SyntaxError as exc:
                attempts += 1
                error_msg = f"{exc.msg} (line {exc.lineno})"
                print(
                    f"[backend_agent] {filename} attempt {attempts}: {error_msg}"
                )
                fixed = _regenerate_python_file(filename, current, error_msg)
                if fixed is None:
                    print(
                        f"[backend_agent] {filename} regen returned no usable code"
                    )
                    break
                current = fixed

        try:
            ast.parse(current)
        except SyntaxError:
            print(
                f"[backend_agent] {filename} still unparseable after "
                f"{_REGEN_MAX_ATTEMPTS} attempts; reverting to original"
            )
            current = original

        # Only safe to regex-rewrite when the file already parses.
        try:
            ast.parse(current)
            current = _fix_async_middleware(current)
        except SyntaxError:
            pass

        if current != original:
            backend_code[filename] = current


def _ensure_response_import(content: str) -> str:
    """
    If a file references `Response(...)` but never imports it from
    fastapi, splice the import in. We check for the *call* form
    (`Response(`) so a local variable named `response` (lowercase, no
    parens) doesn't trigger a spurious patch.

    Order of preference:
      1. Extend an existing `from fastapi import ...` line.
      2. Add a new `from fastapi import Response` line after the last
         import.

    Already-correct files are returned unchanged.
    """
    if not re.search(r"\bResponse\s*\(", content):
        return content
    # Either `from fastapi import ... Response ...` or
    # `from fastapi.responses import Response` counts as "imported".
    if re.search(
        r"^from fastapi(?:\.responses)? import [^\n]*\bResponse\b",
        content,
        re.MULTILINE,
    ):
        return content

    fastapi_import = re.search(
        r"^from fastapi import ([^\n]+)$", content, re.MULTILINE
    )
    if fastapi_import:
        existing = fastapi_import.group(1).rstrip()
        return content.replace(
            fastapi_import.group(0),
            f"from fastapi import {existing}, Response",
            1,
        )

    lines = content.splitlines()
    last_import = -1
    for i, line in enumerate(lines):
        if line.startswith("from ") or line.startswith("import "):
            last_import = i
    insert_at = last_import + 1 if last_import >= 0 else 0
    lines.insert(insert_at, "from fastapi import Response")
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")


def _apply_response_import_fix(backend_code: dict) -> None:
    """Patch every backend .py file that uses `Response(...)` without importing it."""
    for filename, content in list(backend_code.items()):
        if not filename.endswith(".py") or not isinstance(content, str):
            continue
        patched = _ensure_response_import(content)
        if patched != content:
            backend_code[filename] = patched


def _function_missing_row_factory(func: ast.AST) -> bool:
    """
    True if `func` calls `cursor.fetchone()` / `cursor.fetchall()`
    anywhere in its body but never assigns to an attribute named
    `row_factory`. AST walk catches both `conn.row_factory = sqlite3.Row`
    and `connection.row_factory = ...`, regardless of the variable name.
    """
    has_fetch = False
    has_row_factory = False
    for sub in ast.walk(func):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr in ("fetchone", "fetchall"):
                has_fetch = True
        elif isinstance(sub, ast.Assign):
            for target in sub.targets:
                if isinstance(target, ast.Attribute) and target.attr == "row_factory":
                    has_row_factory = True
    return has_fetch and not has_row_factory


def _file_missing_row_factory(content: str) -> bool:
    """True if any top-level function in `content` is missing row_factory."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _function_missing_row_factory(node):
                return True
    return False


def _enforce_row_factory(backend_code: dict) -> None:
    """
    The LLM regularly emits a `database.py` where some helper (typically
    `update_note` / `get_X_by_id`) calls `fetchone()` without setting
    `conn.row_factory = sqlite3.Row` first — making `dict(row)`
    TypeError on the returned tuple, leaving the UPDATE transaction
    uncommitted, and locking the DB for every subsequent request.

    Detect the pattern with AST and ask the LLM to rewrite the file. The
    regen result is only accepted if it parses AND no longer trips the
    same check.
    """
    directive = (
        "Every function in this file that calls cursor.fetchone() or "
        "cursor.fetchall() MUST first set `conn.row_factory = sqlite3.Row` "
        "BEFORE creating the cursor. Without this, fetchone() returns a "
        "tuple and `dict(row)` raises TypeError. Apply this rule to every "
        "such function — including update_*, get_*_by_id, and any "
        "single-row lookup."
    )
    for filename, content in list(backend_code.items()):
        if not filename.endswith(".py") or not isinstance(content, str):
            continue
        if not _file_missing_row_factory(content):
            continue
        print(
            f"[backend_agent] {filename} has fetch calls without "
            "row_factory; regenerating"
        )
        fixed = _regenerate_with_directive(filename, content, directive)
        if fixed is None:
            continue
        try:
            ast.parse(fixed)
        except SyntaxError as exc:
            print(
                f"[backend_agent] row_factory regen for {filename} "
                f"produced SyntaxError ({exc.msg}); discarding"
            )
            continue
        if _file_missing_row_factory(fixed):
            print(
                f"[backend_agent] row_factory regen for {filename} "
                "still misses row_factory; discarding"
            )
            continue
        backend_code[filename] = fixed


def _has_contextmanager_decorator(content: str) -> bool:
    """
    True if any top-level function in `content` is decorated with
    `@contextmanager` (or `@contextlib.contextmanager`). AST-based so it
    doesn't get fooled by the decorator name appearing in a string or
    comment.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name) and dec.id == "contextmanager":
                return True
            if isinstance(dec, ast.Attribute) and dec.attr == "contextmanager":
                return True
    return False


def _regenerate_with_directive(
    filename: str, current_code: str, directive: str
) -> Optional[str]:
    """
    Generic single-file regen with a free-form directive. Used by checks
    that aren't surfacing a Python error (so `_regenerate_python_file`
    with its "SyntaxError: ..." framing would be misleading) but still
    want the LLM to rewrite a specific file.
    """
    try:
        llm = get_llm(temperature=0.1).bind(format="json")
        prompt = (
            f"Rewrite the file `{filename}` to follow this directive:\n\n"
            f"{directive}\n\n"
            f"Return ONLY a JSON object with a single key {json.dumps(filename)} "
            "whose value is the full corrected file contents as a string. "
            "No fences, no commentary.\n\n"
            "Current code:\n"
            f"{current_code}"
        )
        response = llm.invoke([("human", prompt)])
        raw = response.content if hasattr(response, "content") else str(response)
        parsed = json.loads(raw)
        fixed = parsed.get(filename)
        if isinstance(fixed, str) and fixed.strip():
            return fixed
        string_values = [
            v for v in parsed.values() if isinstance(v, str) and v.strip()
        ]
        if len(string_values) == 1:
            return string_values[0]
    except Exception as exc:  # noqa: BLE001
        print(
            f"[backend_agent] directive regen for {filename} failed: {exc}"
        )
    return None


def _enforce_no_contextmanager(backend_code: dict) -> None:
    """
    The LLM keeps emitting `database.py` with `@contextmanager` helpers
    while `main.py` calls them without `with`, blowing up at request
    time as `'_GeneratorContextManager' object has no attribute
    'cursor'`. Detect the pattern statically and ask the LLM to rewrite
    the file as plain functions returning `sqlite3.Connection`.

    Mutates `backend_code` in place. The regen result is only accepted
    if it parses AND no longer carries the decorator.
    """
    directive = (
        "This file uses @contextmanager. Rewrite it WITHOUT @contextmanager "
        "and WITHOUT `from contextlib import contextmanager`. Connection "
        "helpers must be plain functions returning a raw sqlite3.Connection, "
        "e.g. `def get_db() -> sqlite3.Connection: return sqlite3.connect"
        "(\"./app.db\")`. Callers will use the return value directly, not "
        "inside a `with` block."
    )
    for filename, content in list(backend_code.items()):
        if not filename.endswith(".py") or not isinstance(content, str):
            continue
        if not _has_contextmanager_decorator(content):
            continue
        print(
            f"[backend_agent] {filename} uses @contextmanager; regenerating"
        )
        fixed = _regenerate_with_directive(filename, content, directive)
        if fixed is None:
            continue
        try:
            ast.parse(fixed)
        except SyntaxError as exc:
            print(
                f"[backend_agent] contextmanager regen for {filename} "
                f"produced SyntaxError ({exc.msg}); discarding"
            )
            continue
        if _has_contextmanager_decorator(fixed):
            print(
                f"[backend_agent] contextmanager regen for {filename} "
                "still uses @contextmanager; discarding"
            )
            continue
        backend_code[filename] = fixed


def _collect_top_level_names(backend_code: dict) -> Dict[str, List[str]]:
    """
    Map each .py filename to the list of names it defines at module
    level (classes, functions, async functions, simple assignments).
    Fed into the import-error regen prompt so the LLM knows which names
    to import from which file, instead of guessing.
    """
    result: Dict[str, List[str]] = {}
    for fname, content in backend_code.items():
        if not fname.endswith(".py") or not isinstance(content, str):
            continue
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
        names: List[str] = []
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.append(target.id)
        if names:
            result[fname] = names
    return result


def _probe_imports(
    backend_code: dict, entry: str = "main.py"
) -> Optional[Tuple[str, str]]:
    """
    Materialize the backend file map to a temp dir and try to `import
    main` in a subprocess. Returns `(broken_filename, full_stderr)` on
    failure, or `None` if the import succeeds.

    The probe uses a script (not `python -c`) so the temp dir lands on
    `sys.path[0]` and intra-package imports like `from database import
    get_db` resolve naturally.
    """
    if entry not in backend_code:
        return None

    module_name = entry[:-3]

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write every .py file in the backend to disk. Non-.py files
        # (e.g. requirements.txt) are skipped — they can't affect import.
        for fname, content in backend_code.items():
            if not fname.endswith(".py") or not isinstance(content, str):
                continue
            dest = Path(tmpdir) / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        probe_path = Path(tmpdir) / "_probe.py"
        probe_path.write_text(f"import {module_name}\n", encoding="utf-8")

        try:
            result = subprocess.run(
                [sys.executable, str(probe_path)],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=_IMPORT_PROBE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return (entry, f"import probe timed out after {_IMPORT_PROBE_TIMEOUT}s")

        if result.returncode == 0:
            return None

        stderr = (result.stderr or "").strip()

        # Walk the traceback to find the last frame that points to a
        # file in our temp dir — that's the one we want to fix.
        # macOS prepends /private to /var paths in tracebacks, so
        # compare against both raw and resolved tmpdir.
        tmp_real = os.path.realpath(tmpdir)
        broken = entry
        for match in _TRACEBACK_FILE_RE.finditer(stderr):
            path = match.group(1)
            real = os.path.realpath(path)
            if real.startswith(tmp_real) or path.startswith(tmpdir):
                broken = os.path.relpath(real, tmp_real)

        return (broken, stderr or "no stderr")


def _add_module_import(content: str, module: str, name: str) -> str:
    """
    Ensure `from <module> import <name>` exists in `content`. Extends an
    existing `from <module> import ...` line in place, or inserts a new
    import after the last existing one (or at the top if there are none).
    Idempotent: returns `content` unchanged if the import is already there.
    """
    pattern = rf"^from {re.escape(module)} import (.+)$"
    m = re.search(pattern, content, re.MULTILINE)
    if m:
        existing = [n.strip() for n in m.group(1).split(",")]
        if name in existing:
            return content
        new_line = f"from {module} import {', '.join(existing + [name])}"
        return content.replace(m.group(0), new_line, 1)

    lines = content.splitlines()
    last_import = -1
    for i, line in enumerate(lines):
        if line.startswith("from ") or line.startswith("import "):
            last_import = i
    insert_at = last_import + 1 if last_import >= 0 else 0
    lines.insert(insert_at, f"from {module} import {name}")
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")


# Matches the `NameError: name 'X' is not defined` line we see in the
# import-probe traceback. Captures the undefined symbol name.
_NAMEERROR_RE = re.compile(r"NameError: name '(\w+)' is not defined")


def _try_deterministic_import_fix(
    filename: str,
    content: str,
    traceback: str,
    backend_code: dict,
) -> Optional[str]:
    """
    When the probe reports `NameError: name 'X' is not defined` and `X`
    is a top-level definition in another backend file, splice in
    `from <other_module> import X` directly. Returns the patched
    contents on success, or None when no deterministic fix applies
    (different error class, or `X` isn't defined anywhere we know about).

    This bypasses the LLM for the most common probe-failure pattern —
    a referenced model class without the corresponding import — which
    the small repair models routinely fail to fix on their own.
    """
    match = _NAMEERROR_RE.search(traceback or "")
    if not match:
        return None
    missing = match.group(1)

    for other_file, names in _collect_top_level_names(backend_code).items():
        if other_file == filename:
            continue
        if missing not in names:
            continue
        if not other_file.endswith(".py"):
            continue
        module = other_file[:-3].replace("/", ".")
        return _add_module_import(content, module, missing)

    return None


def _regenerate_for_import_error(
    filename: str,
    broken_code: str,
    traceback: str,
    context: Dict[str, List[str]],
) -> Optional[str]:
    """
    Ask the LLM to rewrite a single .py file given an import-time
    traceback. Includes a short summary of what names are defined in
    sibling files so the model can write the correct `from X import Y`
    line, rather than re-emitting the same un-imported reference.
    """
    try:
        llm = get_llm(temperature=0.1).bind(format="json")
        ctx_lines = [
            f"- {fn}: defines {', '.join(names)}"
            for fn, names in context.items()
            if fn != filename
        ]
        ctx_block = "\n".join(ctx_lines) or "(no other backend files)"

        prompt = (
            f"This file `{filename}` failed to import. Full traceback:\n\n"
            f"{traceback}\n\n"
            f"Other backend files in the same directory:\n{ctx_block}\n\n"
            "Fix the broken file. Add any missing imports, define any "
            "missing names, or remove unused references — whatever it "
            "takes to make `import main` succeed.\n\n"
            f"Return ONLY a JSON object with a single key "
            f"{json.dumps(filename)} whose value is the full corrected "
            "file contents as a string. No fences, no commentary.\n\n"
            "Broken code:\n"
            f"{broken_code}"
        )
        response = llm.invoke([("human", prompt)])
        raw = response.content if hasattr(response, "content") else str(response)
        parsed = json.loads(raw)
        fixed = parsed.get(filename)
        if isinstance(fixed, str) and fixed.strip():
            return fixed
        string_values = [
            v for v in parsed.values() if isinstance(v, str) and v.strip()
        ]
        if len(string_values) == 1:
            return string_values[0]
    except Exception as exc:  # noqa: BLE001
        print(f"[backend_agent] import-error regen for {filename} failed: {exc}")
    return None


def _validate_imports(backend_code: dict) -> None:
    """
    Loop: probe `import main` in a subprocess; on failure, ask the LLM
    to repair the file the traceback blames, up to `_REGEN_MAX_ATTEMPTS`
    iterations. Each repaired file is `ast.parse`-checked before being
    accepted, so a regen that introduces a SyntaxError is rejected
    rather than overwriting a parseable (but semantically broken)
    original.

    Mutates `backend_code` in place. Logs each attempt so a CLI run
    surfaces which file failed and why.
    """
    if "main.py" not in backend_code:
        return

    for attempt in range(1, _REGEN_MAX_ATTEMPTS + 1):
        result = _probe_imports(backend_code)
        if result is None:
            return

        broken_file, traceback = result
        last_line = traceback.splitlines()[-1] if traceback else "(no stderr)"
        print(
            f"[backend_agent] import probe attempt {attempt}: "
            f"{broken_file}: {last_line}"
        )

        if broken_file not in backend_code:
            print(
                f"[backend_agent] {broken_file} not in backend_code; "
                "can't repair, stopping"
            )
            return

        # Try the deterministic import-splice first. For
        # `NameError: name 'X' is not defined` where X is a top-level
        # name in some other backend file, we already know the answer
        # — `from <that_file> import X` — so we don't need an LLM round
        # trip. This is the single most common probe failure mode and
        # the small Ollama repair models routinely fail to fix it.
        det = _try_deterministic_import_fix(
            broken_file, backend_code[broken_file], traceback, backend_code
        )
        if det is not None and det != backend_code[broken_file]:
            candidate = dict(backend_code)
            candidate[broken_file] = det
            if _probe_imports(candidate) is None:
                backend_code[broken_file] = det
                print(
                    f"[backend_agent] {broken_file} deterministic "
                    "import-splice applied"
                )
                continue

        context = _collect_top_level_names(backend_code)
        fixed = _regenerate_for_import_error(
            broken_file, backend_code[broken_file], traceback, context
        )
        if fixed is None:
            print("[backend_agent] regen returned no usable code; stopping")
            return

        try:
            ast.parse(fixed)
        except SyntaxError as exc:
            print(
                f"[backend_agent] regen produced SyntaxError "
                f"({exc.msg}); discarding"
            )
            return

        backend_code[broken_file] = fixed

    # One last probe purely for logging — we don't retry past the budget.
    result = _probe_imports(backend_code)
    if result is not None:
        last_line = result[1].splitlines()[-1] if result[1] else "(no stderr)"
        print(
            f"[backend_agent] still failing import after "
            f"{_REGEN_MAX_ATTEMPTS} attempts: {last_line}"
        )


def harden_backend_code(backend_code: dict) -> None:
    """
    Run the full deterministic + LLM-assisted hardening chain over an
    in-memory backend file map. Mutates `backend_code` in place.

    Order matters:
      1. Deterministic patches (Optional / status_code / async middleware)
         go first so downstream checks see code in canonical shape.
      2. AST syntax repair before any other LLM-assisted step — if a file
         doesn't parse, neither `@contextmanager` detection nor the
         import probe can read it correctly.
      3. `@contextmanager` check before the import probe — the probe
         doesn't fail on this pattern, so we'd never catch it otherwise.
      4. `Response` import + `row_factory` checks before the import probe
         too — both can introduce import-time bugs the probe then fixes.
      5. Import probe last among the LLM-assisted steps — it's the
         catch-all for anything the prior checks didn't handle.
      6. Final deterministic re-pass: regens above can drop the typing
         import or the spliced `Response` import; re-running is cheap
         and idempotent.

    Called from both `backend_agent` (original generation) and
    `fixer_agent` (single-file fix). Without invoking this from the
    fixer, its LLM regens routinely re-introduce the same bugs the
    static checks were built to catch.
    """
    _apply_backend_post_processing(backend_code)
    _validate_and_repair_python_files(backend_code)
    _enforce_no_contextmanager(backend_code)
    _apply_response_import_fix(backend_code)
    _enforce_row_factory(backend_code)
    _validate_imports(backend_code)
    _apply_backend_post_processing(backend_code)
    _apply_response_import_fix(backend_code)


def backend_agent(state: AgentState) -> AgentState:
    """Generate the backend file map and store it on state["backend_code"]."""
    architecture = state["architecture"]
    frontend_code = state.get("frontend_code", {})

    # format="json" forces Ollama to emit a parseable JSON object instead of
    # free-form text wrapped in commentary.
    llm = get_llm(temperature=0.1).bind(format="json")

    messages = [
        ("system", _load_system_prompt()),
        ("human", _build_human_message(architecture, frontend_code)),
    ]

    response = llm.invoke(messages)
    raw_text = response.content if hasattr(response, "content") else str(response)

    backend_code = json.loads(raw_text)

    # Deterministic safety net — uvicorn needs fastapi installed and the
    # executor agent runs `pip install -r requirements.txt` blindly. If
    # the LLM forgot the file (or emitted a non-string), drop in a known-
    # good version instead of letting the executor skip the install.
    existing_reqs = backend_code.get("requirements.txt")
    if not isinstance(existing_reqs, str) or not existing_reqs.strip():
        backend_code["requirements.txt"] = _DEFAULT_REQUIREMENTS

    # Run the full hardening chain (deterministic patches + LLM-assisted
    # repairs). Same chain that fixer_agent uses on its own LLM regens,
    # so any fix the fixer applies later in the graph goes through the
    # same gauntlet as the original generation.
    harden_backend_code(backend_code)

    state["backend_code"] = backend_code
    return state
