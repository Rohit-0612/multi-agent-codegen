"""
Executor agent — boots the generated project locally (no Docker) and
verifies that backend + frontend are actually serving traffic.

Deterministic node: no LLM. The reason it exists as an "agent" instead of
a plain core module is symmetry — the LangGraph flow treats it like any
other state-transforming step, and the tester / fixer downstream read its
output dict directly.

Reads:
  - state["output_dir"] — absolute path produced by core/file_writer.py.

Writes:
  - state["execution_result"] = {
        "success": bool,
        "backend_running": bool,
        "frontend_running": bool,
        "backend_pid": int | None,
        "frontend_pid": int | None,
        "logs": str,
        "ports": {"backend": 8000, "frontend": 3000},
    }

Subprocess handles are kept in a module-level dict (_PROCESS_HANDLES)
rather than on AgentState — LangGraph's SQLite checkpointer can't pickle
a live Popen, and stuffing it on state would corrupt the checkpoint.
"""

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional

from core.state import AgentState


# Hard-coded to match the generated docker-compose.yml + frontend dev port.
# If these ever become configurable, pull from state["config_code"] instead.
BACKEND_PORT = 8000
FRONTEND_PORT = 3000

# Sleeps before the first health probe. Tuned for the slowest realistic
# startup on a cold machine; the retry loop covers shorter / longer cases.
_BACKEND_STARTUP_SECONDS = 5
# Vite/CRA with a cold node_modules can take a while on first boot —
# 20s startup + 5 retries (1s apart) covers the slowest realistic case.
_FRONTEND_STARTUP_SECONDS = 20
_HEALTH_RETRIES = 5
_HEALTH_RETRY_SLEEP = 1.0
_HEALTH_TIMEOUT = 3.0

# Module-level handle registry. Kept here so a later cleanup() call (from
# main.py or a test) can find the live processes; main.py is expected to
# call cleanup() in a finally block.
_PROCESS_HANDLES: Dict[str, subprocess.Popen] = {}


def _free_port(port: int, logs: list) -> None:
    """
    Kill anything currently bound to `port`. Uses `lsof -ti tcp:<port>` →
    `kill -9`, which is macOS / Linux specific. Silently no-ops if lsof
    isn't on PATH or the port is already free.
    """
    if shutil.which("lsof") is None:
        logs.append(f"[port {port}] lsof not found, skipping pre-clean")
        return

    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logs.append(f"[port {port}] lsof failed: {exc}")
        return

    pids = [pid.strip() for pid in result.stdout.splitlines() if pid.strip()]
    if not pids:
        return

    logs.append(f"[port {port}] killing existing PIDs: {pids}")
    for pid in pids:
        # SIGKILL is intentional — dev servers often ignore SIGTERM when
        # they're hung, and we need the port back synchronously.
        subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)


def _run_quiet(
    cmd: list, cwd: Path, logs: list, label: str, timeout: int = 300
) -> bool:
    """Run a setup command (pip install / npm install) and log any failure."""
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logs.append(f"[{label}] command failed to start: {exc}")
        return False

    if result.returncode != 0:
        # Tail the stderr instead of dumping multi-MB of npm noise.
        stderr_tail = (result.stderr or "")[-2000:]
        logs.append(f"[{label}] exit={result.returncode}\n{stderr_tail}")
        return False

    logs.append(f"[{label}] ok")
    return True


def _spawn(
    cmd: list,
    cwd: Path,
    label: str,
    logs: list,
    env: Optional[Dict[str, str]] = None,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
) -> Optional[subprocess.Popen]:
    """Start a long-running dev server in the background; return its handle."""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=stdout,
            stderr=stderr,
            # Detach from our process group so a Ctrl-C on the orchestrator
            # doesn't kill the child here — cleanup() handles termination.
            start_new_session=True,
            env=env,
        )
    except (OSError, ValueError) as exc:
        logs.append(f"[{label}] spawn failed: {exc}")
        return None

    logs.append(f"[{label}] spawned pid={proc.pid}")
    return proc


def _tail_log(path: Path, n: int = 50) -> str:
    """Return the last n lines of a log file, or an empty string on error."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _probe_http(url: str, logs: list, label: str) -> bool:
    """
    Issue GET <url> with retries. Returns True on the first 200 response.
    Any other status code, connection error, or timeout counts as failure.

    Always sends `Accept: */*` — urllib doesn't add a default Accept
    header, and Vite returns 404 on `/` when the request omits it
    entirely (it matches a non-HTML internal route). Sending what curl
    sends keeps probe behavior consistent with what a human would see.
    """
    headers = {"Accept": "*/*"}
    for attempt in range(1, _HEALTH_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=_HEALTH_TIMEOUT) as resp:
                if resp.status == 200:
                    logs.append(f"[{label}] healthy (attempt {attempt})")
                    return True
                logs.append(
                    f"[{label}] attempt {attempt} got status {resp.status}"
                )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logs.append(f"[{label}] attempt {attempt} error: {exc}")

        if attempt < _HEALTH_RETRIES:
            time.sleep(_HEALTH_RETRY_SLEEP)

    return False


def _start_backend(
    backend_dir: Path, logs: list, log_path: Optional[Path] = None
) -> Optional[subprocess.Popen]:
    """
    pip install + spawn uvicorn. Returns the Popen on success, None otherwise.

    If `log_path` is provided, uvicorn's stdout+stderr are piped into that
    file — same pattern as _start_frontend. Without it the dev server's
    tracebacks vanish into DEVNULL and "Backend running: False" is
    undebuggable.
    """
    if not backend_dir.is_dir():
        logs.append(f"[backend] directory missing: {backend_dir}")
        return None

    req_file = backend_dir / "requirements.txt"
    if req_file.is_file():
        if not _run_quiet(
            ["pip", "install", "--quiet", "-r", "requirements.txt"],
            cwd=backend_dir,
            logs=logs,
            label="backend pip install",
        ):
            return None
    else:
        logs.append("[backend] no requirements.txt — skipping pip install")

    stdout: object = subprocess.DEVNULL
    stderr: object = subprocess.DEVNULL
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "wb")
            stdout = log_file
            stderr = subprocess.STDOUT
        except OSError as exc:
            logs.append(f"[backend] could not open log file {log_path}: {exc}")

    return _spawn(
        [
            "uvicorn",
            "main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(BACKEND_PORT),
        ],
        cwd=backend_dir,
        label="backend uvicorn",
        logs=logs,
        stdout=stdout,
        stderr=stderr,
    )


def _start_frontend(
    frontend_dir: Path, logs: list, log_path: Optional[Path] = None
) -> Optional[subprocess.Popen]:
    """
    npm install + spawn `npm run dev`. Returns the Popen on success.

    If `log_path` is provided, the dev server's stdout+stderr are piped
    into that file so a failed health probe can inspect what vite/CRA
    actually said. Without this, the dev-server output goes to DEVNULL
    and silent failures (bad config, missing deps) are invisible.
    """
    if not frontend_dir.is_dir():
        logs.append(f"[frontend] directory missing: {frontend_dir}")
        return None

    if shutil.which("npm") is None:
        logs.append("[frontend] npm not on PATH")
        return None

    if not _run_quiet(
        ["npm", "install", "--silent"],
        cwd=frontend_dir,
        logs=logs,
        label="frontend npm install",
        timeout=600,
    ):
        return None

    # PORT covers CRA (which ignores the `--port` arg); BROWSER=none stops
    # CRA from spawning a tab. Vite is fine with either signal.
    frontend_env = {**os.environ, "PORT": str(FRONTEND_PORT), "BROWSER": "none"}

    stdout: object = subprocess.DEVNULL
    stderr: object = subprocess.DEVNULL
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Binary mode + STDOUT merge → single chronological stream.
            # Popen dup's the fd, so we can close our handle right after
            # spawn (done implicitly when this function returns).
            log_file = open(log_path, "wb")
            stdout = log_file
            stderr = subprocess.STDOUT
        except OSError as exc:
            logs.append(f"[frontend] could not open log file {log_path}: {exc}")

    return _spawn(
        ["npm", "run", "dev", "--", "--port", str(FRONTEND_PORT)],
        cwd=frontend_dir,
        label="frontend npm run dev",
        logs=logs,
        env=frontend_env,
        stdout=stdout,
        stderr=stderr,
    )


def cleanup() -> None:
    """
    Terminate any dev servers this agent started. main.py is expected to
    call this in a finally block; safe to call multiple times.
    """
    for label, proc in list(_PROCESS_HANDLES.items()):
        if proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except OSError:
                pass
        _PROCESS_HANDLES.pop(label, None)


def executor_agent(state: AgentState) -> AgentState:
    """Boot backend + frontend locally and probe each over HTTP."""
    logs: list = []
    output_dir = state.get("output_dir")

    # Defensive: if file_writer didn't run (or the key got dropped during
    # a partial replay), don't crash — just report failure cleanly.
    if not output_dir or not Path(output_dir).is_dir():
        logs.append(f"output_dir missing or not a directory: {output_dir!r}")
        state["execution_result"] = {
            "success": False,
            "backend_running": False,
            "frontend_running": False,
            "backend_pid": None,
            "frontend_pid": None,
            "logs": "\n".join(logs),
            "ports": {"backend": BACKEND_PORT, "frontend": FRONTEND_PORT},
        }
        return state

    project_dir = Path(output_dir)

    # Pre-clean both ports so a leftover dev server from a prior run can't
    # mask a freshly-broken backend by answering on 8000.
    _free_port(BACKEND_PORT, logs)
    _free_port(FRONTEND_PORT, logs)

    backend_log_path = project_dir / "backend.log"
    backend_proc = _start_backend(
        project_dir / "backend", logs, log_path=backend_log_path
    )
    if backend_proc is not None:
        _PROCESS_HANDLES["backend"] = backend_proc
        time.sleep(_BACKEND_STARTUP_SECONDS)
        backend_running = _probe_http(
            f"http://localhost:{BACKEND_PORT}/api/health",
            logs,
            label="backend health",
        )
        # Mirror the frontend pattern — tail uvicorn's output into logs
        # when the probe fails so import errors and tracebacks are
        # visible to the caller without digging on disk.
        if not backend_running:
            tail = _tail_log(backend_log_path, n=50)
            if tail:
                logs.append("[backend log tail]\n" + tail)
    else:
        backend_running = False

    frontend_log_path = project_dir / "frontend.log"
    frontend_proc = _start_frontend(
        project_dir / "frontend", logs, log_path=frontend_log_path
    )
    if frontend_proc is not None:
        _PROCESS_HANDLES["frontend"] = frontend_proc
        time.sleep(_FRONTEND_STARTUP_SECONDS)
        frontend_running = _probe_http(
            f"http://localhost:{FRONTEND_PORT}",
            logs,
            label="frontend health",
        )
        # On failure, fold the dev-server's own output into our logs so
        # the caller can see why it died (bad config, port already in
        # use, missing dep, etc.) without having to dig on disk.
        if not frontend_running:
            tail = _tail_log(frontend_log_path, n=50)
            if tail:
                logs.append("[frontend log tail]\n" + tail)
    else:
        frontend_running = False

    state["execution_result"] = {
        "success": backend_running and frontend_running,
        "backend_running": backend_running,
        "frontend_running": frontend_running,
        "backend_pid": backend_proc.pid if backend_proc else None,
        "frontend_pid": frontend_proc.pid if frontend_proc else None,
        "logs": "\n".join(logs),
        "ports": {"backend": BACKEND_PORT, "frontend": FRONTEND_PORT},
    }
    return state
