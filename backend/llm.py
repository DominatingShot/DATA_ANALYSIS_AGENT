"""Thin wrapper over the Anthropic Messages API: one call in, one validated Pydantic model out.

Two ways of getting structured JSON, both driven by the same Pydantic model:

* "structured"  - output_config.format = JSON schema (SDK `messages.parse`). Default.
* "forced_tool" - one tool whose input_schema is the model, tool_choice={"type":"tool"}.
  Claude Opus 5.5 / Sonnet 5.5 / Fable 5.1 reject forced tool_choice with a 400, so this is
  the fallback, used when STRUCTURED_MODE=forced_tool or when structured mode gets a 400.

JSON is never scraped out of free text. A schema-validation failure raises LLMStageError.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

import config
from logging_setup import get_logger
from models import StageTool

log = get_logger("llm")

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=config.LLM_MAX_RETRIES, timeout=config.LLM_TIMEOUT_S)
    return _client


class LLMStageError(Exception):
    """A stage could not produce a valid structured result. `detail` is the real cause."""

    def __init__(self, stage: str, message: str, detail: str, meta: dict | None = None):
        super().__init__(f"[{stage}] {message}: {detail}")
        self.stage = stage
        self.message = message
        self.detail = detail
        self.meta = meta or {}


@dataclass
class LLMCallMeta:
    stage: str
    model: str
    mode: str
    elapsed_ms: int = 0
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    fell_back_from: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, {}, "")}


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: getattr(usage, k, None) for k in keys if getattr(usage, k, None)}


def _system_blocks(instructions: str, cached_context: str | None) -> list[dict[str, Any]]:
    """Stable stage instructions, then (optionally) a large stable context such as schema.md,
    marked for prompt caching. Nothing request-specific goes in the system prompt."""
    blocks: list[dict[str, Any]] = [{"type": "text", "text": instructions}]
    if cached_context:
        blocks.append({"type": "text", "text": cached_context, "cache_control": {"type": "ephemeral"}})
    return blocks


def _common_kwargs(model: str) -> dict[str, Any]:
    if config.REFUSAL_FALLBACKS and model in config.REFUSAL_FALLBACK_MODELS:
        return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    return {}


def _check_stop(stage: str, response: Any, meta: LLMCallMeta) -> None:
    meta.stop_reason = response.stop_reason
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise LLMStageError(stage, "The model declined this request", f"refusal: {details}", meta.as_dict())
    if response.stop_reason == "max_tokens":
        raise LLMStageError(stage, "The model ran out of output space", "stop_reason=max_tokens", meta.as_dict())


def _validate(stage: str, tool: StageTool, data: Any, meta: LLMCallMeta) -> BaseModel:
    try:
        return tool.output_model.model_validate(data)
    except ValidationError as e:
        raise LLMStageError(stage, "The model returned output that breaks the stage contract", str(e), meta.as_dict()) from e


def _call_structured(stage: str, tool: StageTool, model: str, system: list, user: str, effort: str, meta: LLMCallMeta) -> BaseModel:
    client = get_client()
    try:
        response = client.beta.messages.parse(
            model=model,
            max_tokens=config.MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort},
            output_format=tool.output_model,
            **_common_kwargs(model),
        )
    except ValidationError as e:
        # The SDK validates parsed_output with the same Pydantic model.
        raise LLMStageError(stage, "The model returned output that breaks the stage contract", str(e), meta.as_dict()) from e
    meta.usage = _usage_dict(response.usage)
    _check_stop(stage, response, meta)
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise LLMStageError(stage, "The model returned no structured output", f"content types: {[b.type for b in response.content]}", meta.as_dict())
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMStageError(stage, "The model returned invalid JSON", str(e), meta.as_dict()) from e
    return _validate(stage, tool, data, meta)


def tool_definition(tool: StageTool) -> dict[str, Any]:
    """Tool definition derived from the Pydantic model, so the schema is defined once."""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": anthropic.transform_schema(tool.output_model),
    }


def _call_forced_tool(stage: str, tool: StageTool, model: str, system: list, user: str, effort: str, meta: LLMCallMeta) -> BaseModel:
    client = get_client()
    response = client.beta.messages.create(
        model=model,
        max_tokens=config.MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool_definition(tool)],
        tool_choice={"type": "tool", "name": tool.name},
        output_config={"effort": effort},
        **_common_kwargs(model),
    )
    meta.usage = _usage_dict(response.usage)
    _check_stop(stage, response, meta)
    block = next((b for b in response.content if b.type == "tool_use" and b.name == tool.name), None)
    if block is None:
        raise LLMStageError(stage, "The model did not call the required tool", f"content types: {[b.type for b in response.content]}", meta.as_dict())
    return _validate(stage, tool, block.input, meta)


def call_structured(
    *,
    stage: str,
    tool: StageTool,
    model: str,
    instructions: str,
    user: str,
    effort: str,
    cached_context: str | None = None,
) -> tuple[T, dict[str, Any]]:
    """Run one stage. Returns (validated model, call meta). Raises LLMStageError."""
    system = _system_blocks(instructions, cached_context)
    mode = config.STRUCTURED_MODE
    meta = LLMCallMeta(stage=stage, model=model, mode=mode)
    started = time.perf_counter()
    try:
        if mode == "forced_tool":
            result = _call_forced_tool(stage, tool, model, system, user, effort, meta)
        else:
            try:
                result = _call_structured(stage, tool, model, system, user, effort, meta)
            except anthropic.BadRequestError as e:
                log.warning(f"{stage}: structured output rejected ({e.message}); falling back to forced tool call")
                meta.fell_back_from = f"structured: {e.message}"
                meta.mode = "forced_tool"
                result = _call_forced_tool(stage, tool, model, system, user, effort, meta)
    except LLMStageError:
        meta.elapsed_ms = int((time.perf_counter() - started) * 1000)
        raise
    except anthropic.APIStatusError as e:
        meta.elapsed_ms = int((time.perf_counter() - started) * 1000)
        raise LLMStageError(stage, "The language model API returned an error", f"HTTP {e.status_code}: {e.message}", meta.as_dict()) from e
    except anthropic.APIConnectionError as e:
        meta.elapsed_ms = int((time.perf_counter() - started) * 1000)
        raise LLMStageError(stage, "Could not reach the language model API", str(e), meta.as_dict()) from e
    meta.elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.debug(f"{stage}.llm_call", extra={"data": meta.as_dict()})
    return result, meta.as_dict()  # type: ignore[return-value]
