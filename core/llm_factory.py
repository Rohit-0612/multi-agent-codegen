"""
LLM factory — single source of truth for building ChatOllama instances.

Every agent calls get_llm() instead of constructing ChatOllama directly,
so model/base-url changes happen in one place (the .env file).
"""

import os

from dotenv import load_dotenv
from langchain_ollama import ChatOllama

# Load .env once at import time so env vars are available to get_llm().
load_dotenv()


def get_llm(temperature: float = 0.1) -> ChatOllama:
    """
    Build a ChatOllama instance pointed at the local Ollama server.

    temperature defaults to 0.1 — codegen agents want near-deterministic
    output. Callers that need more variety (e.g. brainstorming) can override.
    """
    # Fall back to the project's pinned defaults if .env is missing a key.
    model = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    return ChatOllama(
        model=model,
        base_url=base_url,
        temperature=temperature,
    )
