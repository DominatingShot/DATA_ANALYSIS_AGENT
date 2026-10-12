"""Runtime configuration. Everything tunable lives here, read from the environment / .env."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent
# .env in the repo root; real environment variables win over it.
load_dotenv(ROOT_DIR / ".env", override=False)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


# --- Paths -------------------------------------------------------------------
SCHEMA_PATH = Path(_env("SCHEMA_PATH", str(BACKEND_DIR / "assets/schema.md")))

# --- Database ----------------------------------------------------------------
# Must point at the read-only role (see backend/sql/readonly_role.sql).
DATABASE_URL = _env("DATABASE_URL")
DB_POOL_MIN = int(_env("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(_env("DB_POOL_MAX", "5"))
STATEMENT_TIMEOUT = _env("STATEMENT_TIMEOUT", "10s")
MAX_ROWS = int(_env("MAX_ROWS", "1000"))
# Postgres planner cost units. A full scan + join + group over fact_sales is ~10-30k.
MAX_QUERY_COST = float(_env("MAX_QUERY_COST", "1000000"))

# --- LLM: one model per stage -------------------------------------------------
MODEL_ROUTE = _env("MODEL_ROUTE", "claude-haiku-5-5")
MODEL_VALIDATE = _env("MODEL_VALIDATE", "claude-sonnet-5-51")
MODEL_SQL = _env("MODEL_SQL", "claude-sonnet-5-5")
MODEL_COMPOSE = _env("MODEL_COMPOSE", "claude-sonnet-5-5")

# Effort per stage: low | medium | high | xhigh | max
EFFORT_ROUTE = _env("EFFORT_ROUTE", "low")
EFFORT_VALIDATE = _env("EFFORT_VALIDATE", "medium")
EFFORT_SQL = _env("EFFORT_SQL", "medium")
EFFORT_COMPOSE = _env("EFFORT_COMPOSE", "low")

MAX_TOKENS = int(_env("MAX_TOKENS", "16000"))
LLM_MAX_RETRIES = int(_env("LLM_MAX_RETRIES", "3"))  # transport-level retries (429/5xx/connection)
LLM_TIMEOUT_S = float(_env("LLM_TIMEOUT_S", "120"))

# How structured JSON is obtained from each stage:
#   "structured"  - output_config.format JSON schema (default; works on every current model)
#   "forced_tool" - tool + tool_choice={"type":"tool"} (rejected by Opus 5.5 / Sonnet 5.5 / Fable 5.1)
# In "structured" mode a 400 from the API falls back to forced_tool once.
STRUCTURED_MODE = _env("STRUCTURED_MODE", "structured")

# Server-side refusal fallback (Claude API beta). Applied only to models that support it.
REFUSAL_FALLBACKS = _env("REFUSAL_FALLBACKS", "true").lower() == "true"
REFUSAL_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5"}

# --- Pipeline ------------------------------------------------------------------
MAX_SQL_RETRIES = int(_env("MAX_SQL_RETRIES", "2"))
HISTORY_TURNS = int(_env("HISTORY_TURNS", "5"))
COMPOSE_MAX_ROWS = int(_env("COMPOSE_MAX_ROWS", "300"))
MAX_QUESTION_CHARS = int(_env("MAX_QUESTION_CHARS", "2000"))

# CORS origins for the Vite dev server.
CORS_ORIGINS = (_env("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173") or "").split(",")


def today() -> date:
    """Today's date. ALANKAR_TODAY=YYYY-MM-DD pins it for reproducible tests."""
    pinned = _env("ALANKAR_TODAY")
    return date.fromisoformat(pinned) if pinned else date.today()


def load_schema_md() -> str:
    """schema.md is the single source of truth about the database. Read it, never copy it."""
    return SCHEMA_PATH.read_text(encoding="utf-8")
