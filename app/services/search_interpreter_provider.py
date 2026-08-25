"""Search operations backed by the shared application AI gateway."""

from __future__ import annotations

from typing import Any, Protocol

from app.config import Settings
from app.search_contract import SearchInterpretationModelOutput
from app.services.ai_gateway import (
    AIGateway,
    AIGatewayConfig,
    AIGatewayFailure,
    AIToolCall,
    build_ai_gateway,
    get_ai_gateway,
    inline_local_schema_refs,
    resolve_ai_gateway_config,
    strict_json_schema,
)


SCHEMA_NAME = "search_interpretation"
MAX_COMPLETION_TOKENS = 2_400

# Compatibility aliases keep callers and injected test doubles stable while all
# configuration and transport behavior now live in ``ai_gateway``.
SearchInterpreterProviderFailure = AIGatewayFailure
SearchInterpreterProviderConfig = AIGatewayConfig
SearchAgentToolCall = AIToolCall


class SearchInterpreterProvider(Protocol):
    name: str
    model: str

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SearchInterpretationModelOutput: ...

    async def next_agent_tool_call(
        self,
        *,
        instructions: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> SearchAgentToolCall: ...


class GatewaySearchInterpreterProvider:
    def __init__(self, gateway: AIGateway):
        self._gateway = gateway
        self.name = gateway.name
        self.model = gateway.model

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SearchInterpretationModelOutput:
        output = await self._gateway.generate_structured(
            output_model=SearchInterpretationModelOutput,
            schema_name=SCHEMA_NAME,
            instructions=instructions,
            payload=payload,
            max_output_tokens=MAX_COMPLETION_TOKENS,
        )
        return SearchInterpretationModelOutput.model_validate(output)

    async def next_agent_tool_call(
        self,
        *,
        instructions: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> SearchAgentToolCall:
        return await self._gateway.next_tool_call(
            instructions=instructions,
            messages=messages,
            tools=tools,
            max_output_tokens=MAX_COMPLETION_TOKENS,
        )


def resolve_search_interpreter_provider_config(
    settings: Settings,
) -> SearchInterpreterProviderConfig:
    return resolve_ai_gateway_config(settings)


def build_search_interpreter_provider(
    config: SearchInterpreterProviderConfig,
    *,
    client_factory: Any | None = None,
) -> SearchInterpreterProvider:
    gateway = (
        build_ai_gateway(config)
        if client_factory is None
        else build_ai_gateway(config, client_factory=client_factory)
    )
    return GatewaySearchInterpreterProvider(gateway)


def get_search_interpreter_provider(settings: Settings) -> SearchInterpreterProvider:
    return GatewaySearchInterpreterProvider(get_ai_gateway(settings))
