# Project: Multi-Agent Code Generation System

## What This Project Does
User ek prompt deta hai → System automatically:
1. Code generate karta hai (React + FastAPI + LangChain)
2. Docker mein locally run karta hai
3. Tests generate + run karta hai
4. Self-fix karta hai agar tests fail hon
Output = working app on localhost. Fully local, no internet.

## Tech Stack
- LangGraph
- LangChain + ChatOllama
- Model: qwen2.5-coder:7b
- Persistence: langgraph-checkpoint-sqlite
- Docker + subprocess
- Python 3.11+

## Key Design Decisions (DO NOT CHANGE)
- Sequential agents — NOT parallel
- Single model for all agents
- LLM-based reviewer — NOT string matching
- total=False in AgentState
- retry_count and fix_attempts = 0 in main.py
- Ollama only — fully local

## LangGraph Flow
START → orchestrator → frontend → backend → ai_agent → reviewer
reviewer → {
    pass           : file_writer
    retry_frontend : frontend
    retry_backend  : backend
    retry_ai       : ai_agent
    failed         : END
}
file_writer → executor → tester → {
    pass   : END
    fix    : fixer → executor
    failed : END
}

## Build Order
- Step 1  → core/state.py           ⏳
- Step 2  → core/llm_factory.py     ⏳
- Step 3  → agents/orchestrator.py  ⏳
- Step 4  → core/graph.py           ⏳
- Step 5  → agents/frontend_agent.py ⏳
- Step 6  → agents/backend_agent.py ⏳
- Step 7  → agents/ai_agent.py      ⏳
- Step 8  → agents/reviewer_agent.py ⏳
- Step 9  → core/file_writer.py     ⏳
- Step 10 → agents/executor_agent.py ⏳
- Step 11 → agents/tester_agent.py  ⏳
- Step 12 → agents/fixer_agent.py   ⏳
- Step 13 → main.py                 ⏳

## Session Start — Claude Code Ko Yeh Karna Hai
1. work/progress.md dekho — kahan tak bana
2. Wahan se continue karo

## Session End — Claude Code Ko Yeh Karna Hai
1. work/progress.md update karo
2. Koi error fix hua → work/errors.md mein note karo