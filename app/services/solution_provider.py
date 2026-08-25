"""Build operations backed by the shared application AI gateway."""

from __future__ import annotations

from typing import Protocol

from app.config import Settings
from app.services.ai_gateway import AIGateway, get_ai_gateway
from app.solution_contract import SolutionModelOutput


class SolutionProvider(Protocol):
    name: str
    model: str

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SolutionModelOutput: ...


class GatewaySolutionProvider:
    def __init__(self, gateway: AIGateway, max_output_tokens: int):
        self._gateway = gateway
        self._max_output_tokens = max(2_000, min(int(max_output_tokens), 20_000))
        self.name = gateway.name
        self.model = gateway.model

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SolutionModelOutput:
        output = await self._gateway.generate_structured(
            output_model=SolutionModelOutput,
            schema_name="solution_case",
            instructions=instructions,
            payload=payload,
            max_output_tokens=self._max_output_tokens,
        )
        return SolutionModelOutput.model_validate(output)


def get_solution_provider(settings: Settings) -> SolutionProvider:
    return GatewaySolutionProvider(
        get_ai_gateway(settings),
        settings.solution_max_output_tokens,
    )
