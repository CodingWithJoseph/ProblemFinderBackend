"""Analyze operations backed by the shared application AI gateway."""

from __future__ import annotations

from typing import Any, Protocol

from app.config import Settings
from app.services.ai_gateway import (
    AIGateway,
    AIGatewayConfig,
    AIGatewayFailure,
    build_ai_gateway,
    get_ai_gateway,
    resolve_ai_gateway_config,
)
from app.signal_contract import (
    SignalAnalysisModelOutput,
    SignalConversationModelOutput,
    SignalProblemAnalysisOutput,
)


SIGNAL_MAX_COMPLETION_TOKENS = 8_000
CONVERSATION_MAX_COMPLETION_TOKENS = 2_500

SignalAnalysisProviderFailure = AIGatewayFailure
SignalAnalysisProviderConfig = AIGatewayConfig


class SignalAnalysisProvider(Protocol):
    name: str
    model: str

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SignalAnalysisModelOutput: ...

    async def analyze_problems(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SignalProblemAnalysisOutput: ...

    async def ask(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SignalConversationModelOutput: ...


class GatewaySignalAnalysisProvider:
    def __init__(self, gateway: AIGateway):
        self._gateway = gateway
        self.name = gateway.name
        self.model = gateway.model

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SignalAnalysisModelOutput:
        output = await self._gateway.generate_structured(
            output_model=SignalAnalysisModelOutput,
            schema_name="signal_analysis",
            instructions=instructions,
            payload=payload,
            max_output_tokens=SIGNAL_MAX_COMPLETION_TOKENS,
        )
        return SignalAnalysisModelOutput.model_validate(output)

    async def analyze_problems(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SignalProblemAnalysisOutput:
        output = await self._gateway.generate_structured(
            output_model=SignalProblemAnalysisOutput,
            schema_name="signal_problem_analysis",
            instructions=instructions,
            payload=payload,
            max_output_tokens=SIGNAL_MAX_COMPLETION_TOKENS,
        )
        return SignalProblemAnalysisOutput.model_validate(output)

    async def ask(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SignalConversationModelOutput:
        output = await self._gateway.generate_structured(
            output_model=SignalConversationModelOutput,
            schema_name="signal_conversation",
            instructions=instructions,
            payload=payload,
            max_output_tokens=CONVERSATION_MAX_COMPLETION_TOKENS,
        )
        return SignalConversationModelOutput.model_validate(output)


def resolve_signal_analysis_provider_config(
    settings: Settings,
) -> SignalAnalysisProviderConfig:
    return resolve_ai_gateway_config(settings)


def build_signal_analysis_provider(
    config: SignalAnalysisProviderConfig,
    *,
    client_factory: Any | None = None,
) -> SignalAnalysisProvider:
    gateway = (
        build_ai_gateway(config)
        if client_factory is None
        else build_ai_gateway(config, client_factory=client_factory)
    )
    return GatewaySignalAnalysisProvider(gateway)


def get_signal_analysis_provider(settings: Settings) -> SignalAnalysisProvider:
    return GatewaySignalAnalysisProvider(get_ai_gateway(settings))
