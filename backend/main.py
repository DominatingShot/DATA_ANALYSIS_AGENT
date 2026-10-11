"""FastAPI app.

    POST /api/chat          -> ChatResponse (blocking)
    POST /api/chat/stream   -> Server-Sent Events: stage events, then the ChatResponse
    GET  /api/health        -> DB connectivity and read-only status
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

import config
import orchestrator
from logging_setup import get_logger
from models import ChatRequest, ChatResponse
from tools import db

log = get_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.SCHEMA_PATH.exists():
        raise RuntimeError(f"schema.md not found at {config.SCHEMA_PATH}")
    config.load_schema_md()
    if config.DATABASE_URL:
        try:
            catalog = db.introspect_catalog()
            log.info(f"catalog loaded: { {t: len(c) for t, c in catalog.items()} }")
        except Exception as e:  # keep serving; /api/health shows the cause
            log.error(f"database not reachable at startup: {type(e).__name__}: {e}")
    else:
        log.warning("DATABASE_URL is not set; database questions will fail until it is")
    yield
    db.close_pool()


app = FastAPI(title="Alankar Hotel Analytics Platform", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in config.CORS_ORIGINS if o],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/health")
def health() -> dict:
    database = db.health()
    return {
        "ok": bool(database.get("connected")) and bool(database.get("role_read_only")),
        "database": database,
        "schema_md": config.SCHEMA_PATH.exists(),
        "models": {
            "route": config.MODEL_ROUTE,
            "validate": config.MODEL_VALIDATE,
            "sql": config.MODEL_SQL,
            "compose": config.MODEL_COMPOSE,
        },
    }


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    # Sync handler: FastAPI runs it in a worker thread, so the blocking SDK/DB calls are fine.
    return orchestrator.answer(req.message, req.history)


def _sse(events: Iterator[dict]) -> Iterator[str]:
    for event in events:
        if event["event"] == "result":
            payload = {"event": "result", "data": event["data"].model_dump(mode="json")}
        else:
            payload = event
        yield f"event: {payload['event']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _sse(orchestrator.run(req.message, req.history)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
