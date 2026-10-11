"""Structured JSON-lines logging. Every stage's input and output is logged at DEBUG."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

import config

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id.get(),
            "msg": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if data is not None:
            payload["data"] = data
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("alankar")
    root.setLevel(config.LOG_LEVEL)
    root.propagate = False

    file_handler = logging.FileHandler(config.LOG_DIR / "alankar.jsonl", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(f"alankar.{name}")


def log_stage(logger: logging.Logger, stage: str, event: str, data: object) -> None:
    """event is "input" or "output" (or "error"); data must be JSON-serialisable or pydantic."""
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    logger.debug(f"{stage}.{event}", extra={"data": data})
