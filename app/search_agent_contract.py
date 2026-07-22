"""Public, reasoning-free contract for bounded Search agent execution."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.search_contract import SearchInterpretationResponse


SearchAgentAction = Literal[
    "inspect_filter_options",
    "prepare_search_draft",
    "unsupported_tool",
]
SearchAgentStepOutcome = Literal["completed", "rejected"]
SearchAgentStopReason = Literal[
    "confirmation_required",
    "clarification_required",
    "fallback",
    "step_limit",
]


class SearchAgentStep(BaseModel):
    """Safe execution metadata; never model reasoning or hidden analysis."""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1, le=3)
    action: SearchAgentAction
    outcome: SearchAgentStepOutcome


class SearchAgentResponse(SearchInterpretationResponse):
    """A confirmable interpretation plus its bounded, public action trace."""

    model_config = ConfigDict(extra="forbid")

    steps: list[SearchAgentStep] = Field(max_length=3)
    stop_reason: SearchAgentStopReason
