# multi-agent-codegen

A multi-agent code generation pipeline that turns a natural-language prompt
into a runnable full-stack app (React frontend + FastAPI backend) and then
starts, tests, and self-repairs it.

Built on LangGraph with a local Ollama model.

## Pipeline

```
orchestrator → frontend ─┐
                         ├→ reviewer → file_writer → executor → tester
              backend  ──┤                                         │
              ai_agent ──┘                                         │
                              ┌────────────────────────────────────┘
                              ▼
                            fixer ─→ executor ─→ tester  (loop, max 3)
```

Each node is its own agent (`agents/*.py`) with its own system prompt
(`prompts/*.md`). The graph is compiled in `core/graph.py` with a
`SqliteSaver` checkpointer so partial runs can resume across processes.

## What gets generated

For a prompt like `"Build me a todo app with React frontend and FastAPI
backend"`, the pipeline writes a project folder under `output/<name>/`:

```
output/todo_app/
  frontend/      React + Tailwind + Vite
  backend/       FastAPI + SQLite
  ai/            Optional LangChain layer (only when the prompt asks for it)
  config/        docker-compose.yml, .env.example
  README.md      Quickstart for the generated app
  manifest.json  File list + timestamp
```

And a matching `output/<name>.zip`.

## Hardening

The backend agent ships a layered repair chain via `harden_backend_code()`
that runs after the LLM generates the file map. The same chain is invoked
from `fixer_agent` so test-failure repairs go through the same gauntlet as
original generation.

Deterministic patches (no LLM cost):

- `Optional` typing import scrubbing — strips wrong `from pydantic import
  Optional`, adds `from typing import Optional` when needed
- `: str = None` / `: int = None` → `: Optional[str] = None` / `Optional[int]`
- `status_code=201` on POST decorators, `status_code=204` on DELETE
- `Response` import splice when `Response(...)` is referenced but not imported
- Async middleware promotion (`def f(req, call_next)` → `async def` when the
  body uses `await call_next`)
- Deterministic import splice — for `NameError: name 'X' is not defined`
  where `X` is defined in another backend file, splice `from <that_file>
  import X` directly without an LLM round-trip
- `requirements.txt` fallback when the model forgets to emit it

LLM-assisted repairs (only fire when a bug pattern is detected):

- AST syntax repair — `ast.parse` every `.py` file; on `SyntaxError`, ask
  the LLM to rewrite the file (max 2 attempts, discard regens that don't
  parse, revert to original if all attempts fail)
- `@contextmanager` detection — when `database.py` decorates a connection
  helper with `@contextmanager` but `main.py` calls it without `with`,
  force a rewrite as plain functions returning `sqlite3.Connection`
- `row_factory` AST check — any function calling `fetchone()`/`fetchall()`
  without setting `conn.row_factory = sqlite3.Row` gets regenerated with a
  directive prompt
- Subprocess import probe — write backend files to a temp dir, run
  `import main` in a subprocess, parse the traceback, hand the broken
  file + traceback + neighbor-file top-level names to the LLM

Tester:

- Schema-aware — reads the actual Pydantic classes from `backend_code/
  models.py` and feeds them to the test-plan LLM so POST/PUT payloads
  match the real request shape instead of being guessed from endpoint
  descriptions.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.ai/) running locally with `qwen2.5-coder:7b`
  pulled (or any model that supports JSON-formatted output via `format=json`)
- Node + npm (only needed if you want to run the generated frontend)

## Setup

```bash
git clone https://github.com/Rohit-0612/multi-agent-codegen.git
cd multi-agent-codegen
pip install -r requirements.txt
```

Create a `.env` (or rely on defaults):

```
OLLAMA_MODEL=qwen2.5-coder:7b
OLLAMA_BASE_URL=http://localhost:11434
```

Then make sure Ollama is up:

```bash
ollama serve &
ollama pull qwen2.5-coder:7b
```

## Run

```bash
python3 main.py "Build me a todo app with React frontend and FastAPI backend"
```

You'll see phase banners as the graph progresses:

```
[1/5] Planning architecture...
[2/5] Generating code...
[3/5] Reviewing code...
[4/5] Writing files...
[5/5] Running and testing...

================================
MULTI-AGENT CODEGEN — COMPLETE
================================
Status      : success
Output dir  : output/todo_app
ZIP         : output/todo_app.zip
Tests       : 5/5 passed
================================
```

`Status` is one of `success`, `tests_failed`, `execution_failed`,
`review_failed`, `no_tests_run`, or `execution_skipped`. Final test counts
come from `tester_agent`, which hits the live backend with
`urllib.request`.

## Project layout

```
.
├── main.py                  # CLI entry point, drives the LangGraph stream
├── core/
│   ├── graph.py             # Workflow wiring + retry caps
│   ├── state.py             # AgentState TypedDict
│   ├── llm_factory.py       # Single ChatOllama factory
│   └── file_writer.py       # Materializes state → disk + zip
├── agents/
│   ├── orchestrator.py      # Builds architecture plan from the prompt
│   ├── frontend_agent.py    # React + Tailwind generation
│   ├── backend_agent.py     # FastAPI + SQLite generation + hardening
│   ├── ai_agent.py          # Optional LangChain layer
│   ├── reviewer_agent.py    # Contract-mismatch audit
│   ├── executor_agent.py    # Starts uvicorn / vite in background
│   ├── tester_agent.py      # Schema-aware HTTP test plan
│   └── fixer_agent.py       # Single-file LLM repair driven by failures
└── prompts/                 # System prompt per agent
```

Generated apps land in `output/<name>/` and matching `output/<name>.zip`.
Both are gitignored.
