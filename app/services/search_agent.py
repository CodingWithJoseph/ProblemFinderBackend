"""Bounded, allow-listed tool loop for conversational Search."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.prompts.search_agent import SEARCH_AGENT_INSTRUCTIONS
from app.search_agent_contract import SearchAgentResponse, SearchAgentStep, SearchAgentStopReason
from app.search_contract import (
    SearchFilterOptions,
    SearchInterpretationModelOutput,
    SearchInterpretRequest,
)
from app.services.search_interpreter import (
    _fallback_response,
    _response,
    _validated_model_output,
    load_search_filter_options,
)
from app.services.search_interpreter_provider import (
    SearchInterpreterProviderFailure,
    get_search_interpreter_provider,
    inline_local_schema_refs,
    strict_json_schema,
)


logger = logging.getLogger(__name__)

SEARCH_AGENT_MAX_STEPS = 3
FILTER_OPTIONS_TOOL = "get_search_filter_options"
PREPARE_DRAFT_TOOL = "prepare_search_draft"


def _agent_tools() -> list[dict[str, Any]]:
    interpretation_schema = inline_local_schema_refs(
        strict_json_schema(SearchInterpretationModelOutput)
    )
    return [
        {
            "type": "function",
            "function": {
                "name": FILTER_OPTIONS_TOOL,
                "description": (
                    "Read the canonical opportunity domains, opportunity types, "
                    "sources, and communities available to structured search filters."
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": PREPARE_DRAFT_TOOL,
                "description": (
                    "Finish the agent turn with a complete, confirmable search draft. "
                    "This tool never executes the search."
                ),
                "strict": True,
                "parameters": interpretation_schema,
            },
        },
    ]


def _tool_result(call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, ensure_ascii=False),
    }


def _safe_validation_issues(error: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "path": ".".join(str(part) for part in issue.get("loc", [])),
            "type": str(issue.get("type", "invalid")),
        }
        for issue in error.errors()[:10]
    ]


def _with_trace(
    interpretation,
    steps: list[SearchAgentStep],
    stop_reason: SearchAgentStopReason,
) -> SearchAgentResponse:
    return SearchAgentResponse(
        **interpretation.model_dump(),
        steps=steps,
        stop_reason=stop_reason,
    )


def _provider_name() -> str:
    provider = get_settings().search_interpreter_provider.strip().lower()
    return provider if provider in {"local", "groq"} else "invalid"


async def run_search_agent(
    request: SearchInterpretRequest,
    uid: str,
    db: AsyncSession,
) -> SearchAgentResponse:
    """Run a maximum of three safe tool calls and stop before query execution."""
    settings = get_settings()
    provider_name = _provider_name()
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "current_utc": datetime.now(timezone.utc).isoformat(),
                    "current_draft": (
                        request.current_draft.model_dump(mode="json")
                        if request.current_draft
                        else None
                    ),
                    "latest_user_message": request.message,
                    "confirmation_required": True,
                },
                ensure_ascii=False,
            ),
        }
    ]
    tools = _agent_tools()
    options: SearchFilterOptions | None = None
    steps: list[SearchAgentStep] = []

    try:
        # User identity remains inside the application and is never sent to a provider.
        _ = uid
        provider = get_search_interpreter_provider(settings)
        for sequence in range(1, SEARCH_AGENT_MAX_STEPS + 1):
            call = await provider.next_agent_tool_call(
                instructions=SEARCH_AGENT_INSTRUCTIONS,
                messages=messages,
                tools=tools,
            )
            messages.append(call.assistant_message)

            if call.name == FILTER_OPTIONS_TOOL:
                if call.arguments:
                    steps.append(
                        SearchAgentStep(
                            sequence=sequence,
                            action="inspect_filter_options",
                            outcome="rejected",
                        )
                    )
                    messages.append(
                        _tool_result(
                            call.call_id,
                            {
                                "error": "arguments_not_allowed",
                                "instruction": f"Call {FILTER_OPTIONS_TOOL} with an empty object.",
                            },
                        )
                    )
                    continue
                try:
                    if options is None:
                        options = await load_search_filter_options(db)
                except SQLAlchemyError:
                    await db.rollback()
                    steps.append(
                        SearchAgentStep(
                            sequence=sequence,
                            action="inspect_filter_options",
                            outcome="rejected",
                        )
                    )
                    logger.warning(
                        "search agent fallback provider=%s category=options_unavailable",
                        provider_name,
                    )
                    return _with_trace(
                        _fallback_response(request, SearchFilterOptions()),
                        steps,
                        "fallback",
                    )

                steps.append(
                    SearchAgentStep(
                        sequence=sequence,
                        action="inspect_filter_options",
                        outcome="completed",
                    )
                )
                messages.append(
                    _tool_result(
                        call.call_id,
                        {"available_options": options.model_dump(mode="json")},
                    )
                )
                continue

            if call.name == PREPARE_DRAFT_TOOL:
                if options is None:
                    steps.append(
                        SearchAgentStep(
                            sequence=sequence,
                            action="prepare_search_draft",
                            outcome="rejected",
                        )
                    )
                    messages.append(
                        _tool_result(
                            call.call_id,
                            {
                                "error": "filter_options_required",
                                "instruction": f"Call {FILTER_OPTIONS_TOOL} before preparing a draft.",
                            },
                        )
                    )
                    continue
                try:
                    output = SearchInterpretationModelOutput.model_validate(call.arguments)
                except ValidationError as exc:
                    steps.append(
                        SearchAgentStep(
                            sequence=sequence,
                            action="prepare_search_draft",
                            outcome="rejected",
                        )
                    )
                    messages.append(
                        _tool_result(
                            call.call_id,
                            {
                                "error": "invalid_draft",
                                "issues": _safe_validation_issues(exc),
                            },
                        )
                    )
                    continue

                validated = _validated_model_output(output, request, options)
                interpretation = _response(validated, options, fallback_used=False)
                steps.append(
                    SearchAgentStep(
                        sequence=sequence,
                        action="prepare_search_draft",
                        outcome="completed",
                    )
                )
                stop_reason: SearchAgentStopReason = (
                    "clarification_required"
                    if interpretation.needs_clarification
                    else "confirmation_required"
                )
                return _with_trace(interpretation, steps, stop_reason)

            steps.append(
                SearchAgentStep(
                    sequence=sequence,
                    action="unsupported_tool",
                    outcome="rejected",
                )
            )
            messages.append(
                _tool_result(
                    call.call_id,
                    {
                        "error": "unsupported_tool",
                        "allowed_tools": [FILTER_OPTIONS_TOOL, PREPARE_DRAFT_TOOL],
                    },
                )
            )
    except SearchInterpreterProviderFailure as exc:
        logger.warning(
            "search agent fallback provider=%s category=%s",
            provider_name,
            exc.category,
        )
        return _with_trace(
            _fallback_response(request, options or SearchFilterOptions()),
            steps,
            "fallback",
        )
    except Exception:
        logger.warning(
            "search agent fallback provider=%s category=unexpected",
            provider_name,
        )
        return _with_trace(
            _fallback_response(request, options or SearchFilterOptions()),
            steps,
            "fallback",
        )

    logger.warning(
        "search agent fallback provider=%s category=step_limit",
        provider_name,
    )
    return _with_trace(
        _fallback_response(request, options or SearchFilterOptions()),
        steps,
        "step_limit",
    )
