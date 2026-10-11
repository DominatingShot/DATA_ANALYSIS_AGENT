"""Thin wrapper over the Anthropic Messages API: one call in, one validated Pydantic model out.

Two ways of getting structured JSON, both driven by the same Pydantic model:

* "structured"  - output_config.format = JSON schema (SDK `messages.parse`). Default.
* "forced_tool" - one tool whose input_schema is the model, tool_choice={"type":"tool"}.
  Claude Opus 5.5 / Sonnet 5.5 / Fable 5.1 reject forced tool_choice with a 400, so this is
  the fallback, used when STRUCTURED_MODE=forced_tool or when structured mode gets a 400.
"""

from __future__ import annotations

from functools import cache
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

import config
from models import StageTool

T = TypeVar("T", bound=BaseModel)


@cache
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=config.LLM_MAX_RETRIES, timeout=config.LLM_TIMEOUT_S)


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


def _call_structured(tool: StageTool, model: str, system: list, user: str, effort: str) -> BaseModel:
    response = get_client().beta.messages.parse(
        model=model,
        max_tokens=config.MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort},
        output_format=tool.output_model,
        **_common_kwargs(model),
    )
    text = next(b.text for b in response.content if b.type == "text")
    return tool.output_model.model_validate_json(text)


def tool_definition(tool: StageTool) -> dict[str, Any]:
    """Tool definition derived from the Pydantic model, so the schema is defined once."""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": anthropic.transform_schema(tool.output_model),
    }


def _call_forced_tool(tool: StageTool, model: str, system: list, user: str, effort: str) -> BaseModel:
    response = get_client().beta.messages.create(
        model=model,
        max_tokens=config.MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool_definition(tool)],
        tool_choice={"type": "tool", "name": tool.name},
        output_config={"effort": effort},
        **_common_kwargs(model),
    )
    block = next(b for b in response.content if b.type == "tool_use" and b.name == tool.name)
    return tool.output_model.model_validate(block.input)


def call_structured(
    *,
    tool: StageTool,
    model: str,
    instructions: str,
    user: str,
    effort: str,
    cached_context: str | None = None,
) -> T:
    """Run one stage and return the validated model."""
    system = _system_blocks(instructions, cached_context)
    if config.STRUCTURED_MODE == "forced_tool":
        return _call_forced_tool(tool, model, system, user, effort)  # type: ignore[return-value]
    try:
        return _call_structured(tool, model, system, user, effort)  # type: ignore[return-value]
    except anthropic.BadRequestError:
        return _call_forced_tool(tool, model, system, user, effort)  # type: ignore[return-value]
