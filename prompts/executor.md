# Executor Agent — Notes

**No LLM is involved.** This file exists for parity with the other agents
and to document the contract; nothing in `executor_agent.py` reads it at
runtime.

## What the agent does

Takes the generated project sitting at `state["output_dir"]` and tries to
boot it locally on the host machine — no Docker, no containers — using
plain `subprocess.Popen`. Then probes each service over HTTP to confirm
it's actually serving traffic.

## Sequence

1. **Free the ports.** Anything already listening on 8000 (backend) or
   3000 (frontend) is killed via `lsof -ti tcp:<port>` → `kill -9`.
   Re-running the pipeline can't be blocked by a leftover dev server.
2. **Backend.**
   - `pip install -r requirements.txt` (quiet) inside `output_dir/backend`.
   - Spawn `uvicorn main:app --host 0.0.0.0 --port 8000` as a background
     subprocess.
   - Sleep 5 seconds for startup, then probe `GET /api/health` with up to
     3 retries (1s apart). 200 = healthy.
3. **Frontend.**
   - `npm install --silent` inside `output_dir/frontend`.
   - Spawn `npm run dev -- --port 3000` as a background subprocess.
   - Sleep 8 seconds for startup, then probe `GET /` with up to 3 retries
     (1s apart). 200 = healthy.

## Failure handling

Any failure — missing `output_dir`, missing install commands, install
errors, spawn errors, health probe timeouts — is captured into the logs
and reflected in `success: False`. The agent never raises; the graph
keeps moving so the tester / fixer can react.

## State output

```
state["execution_result"] = {
  "success":          bool,
  "backend_running":  bool,
  "frontend_running": bool,
  "backend_pid":      int | None,
  "frontend_pid":     int | None,
  "logs":             str,
  "ports":            {"backend": 8000, "frontend": 3000}
}
```

## Process handles

`Popen` objects are stashed in a module-level `_PROCESS_HANDLES` dict
(keyed by `"backend"` / `"frontend"`) so a later cleanup hook in `main.py`
can terminate them. They're deliberately NOT placed on `AgentState` —
LangGraph checkpoints would try to serialize them and choke on the
non-picklable handle.
