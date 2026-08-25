import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.services.ai_gateway import (
    LOCAL_BASE_URL,
    LOCAL_DEFAULT_MODEL,
    OPENAI_BASE_URL,
    OPENAI_DEFAULT_MODEL,
    AIGatewayFailure,
    build_ai_gateway,
    resolve_ai_gateway_config,
)


class ExampleOutput(BaseModel):
    title: str
    tags: list[str]


class SequencedChatCompletions:
    def __init__(self, contents=None, tool_calls=None, error=None):
        self.contents = list(contents or [])
        self.tool_calls = list(tool_calls or [])
        self.error = error
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        content = self.contents.pop(0) if self.contents else None
        tool_calls = self.tool_calls.pop(0) if self.tool_calls else None
        message = SimpleNamespace(content=content, refusal=None, tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class SequencedResponses:
    def __init__(self, contents):
        self.contents = list(contents)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(output_text=self.contents.pop(0), output=[])


class FakeClientFactory:
    def __init__(self, *, contents=None, response_contents=None, tool_calls=None, error=None):
        self.chat_completions = SequencedChatCompletions(contents, tool_calls, error)
        self.responses = SequencedResponses(response_contents or [])
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=self.chat_completions),
            responses=self.responses,
        )
        self.options = None

    def __call__(self, **kwargs):
        self.options = kwargs
        return self.client


def settings(**overrides):
    values = {
        "ai_provider": "local",
        "ai_base_url": "",
        "ai_model": "",
        "ai_api_key": "",
        "ai_timeout_seconds": 120.0,
        "ai_reasoning_effort": "medium",
        "environment": "development",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def assert_strict_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties", {})
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(properties)
        assert "default" not in node
        for value in node.values():
            assert_strict_objects(value)
    elif isinstance(node, list):
        for value in node:
            assert_strict_objects(value)


def test_gateway_configuration_uses_local_server_for_development():
    config = resolve_ai_gateway_config(settings())

    assert config.name == "local"
    assert config.base_url == LOCAL_BASE_URL
    assert config.model == LOCAL_DEFAULT_MODEL
    assert config.api_key == "local-not-a-secret"


def test_gateway_configuration_uses_only_openai_in_production():
    with pytest.raises(AIGatewayFailure) as local_error:
        resolve_ai_gateway_config(settings(environment="production"))
    assert local_error.value.category == "invalid_configuration"

    config = resolve_ai_gateway_config(settings(
        environment="production",
        ai_provider="openai",
        ai_api_key="test-key",
    ))
    assert config.name == "openai"
    assert config.base_url == OPENAI_BASE_URL
    assert config.model == OPENAI_DEFAULT_MODEL


def test_openai_configuration_requires_credentials_and_official_host():
    with pytest.raises(AIGatewayFailure) as missing:
        resolve_ai_gateway_config(settings(ai_provider="openai"))
    assert missing.value.category == "missing_credentials"

    with pytest.raises(AIGatewayFailure) as unsafe_host:
        resolve_ai_gateway_config(settings(
            ai_provider="openai",
            ai_api_key="test-key",
            ai_base_url="https://example.com/v1",
        ))
    assert unsafe_host.value.category == "invalid_configuration"


async def test_local_structured_output_uses_strict_schema_and_repairs_once():
    factory = FakeClientFactory(contents=["{}", '{"title":"Ready","tags":[]}'])
    config = resolve_ai_gateway_config(settings(
        ai_base_url="http://host.docker.internal:8080/v1",
        ai_model="local-model",
    ))
    gateway = build_ai_gateway(config, client_factory=factory)

    result = await gateway.generate_structured(
        output_model=ExampleOutput,
        schema_name="example_output",
        instructions="Return the contract.",
        payload={"source": "cluster"},
        max_output_tokens=2_400,
    )

    assert result == ExampleOutput(title="Ready", tags=[])
    assert factory.options == {
        "api_key": "local-not-a-secret",
        "base_url": "http://host.docker.internal:8080/v1",
        "timeout": 120.0,
        "max_retries": 0,
    }
    assert len(factory.chat_completions.requests) == 2
    request = factory.chat_completions.requests[0]
    assert request["model"] == "local-model"
    assert request["max_tokens"] == 2_400
    assert "max_completion_tokens" not in request
    response_format = request["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["name"] == "example_output"
    schema = response_format["json_schema"]["schema"]
    assert "$defs" not in schema
    assert "$ref" not in str(schema)
    assert_strict_objects(schema)


async def test_openai_structured_output_uses_responses_api_and_cost_controls():
    factory = FakeClientFactory(response_contents=['{"title":"Ready","tags":[]}'])
    config = resolve_ai_gateway_config(settings(
        ai_provider="openai",
        ai_api_key="test-key",
        ai_reasoning_effort="high",
    ))
    gateway = build_ai_gateway(config, client_factory=factory)

    result = await gateway.generate_structured(
        output_model=ExampleOutput,
        schema_name="example_output",
        instructions="Return the contract.",
        payload={"source": "cluster"},
        max_output_tokens=8_000,
    )

    assert result.title == "Ready"
    request = factory.responses.requests[0]
    assert request["model"] == OPENAI_DEFAULT_MODEL
    assert request["max_output_tokens"] == 8_000
    assert request["reasoning"] == {"effort": "high"}
    assert request["store"] is False
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert_strict_objects(request["text"]["format"]["schema"])


@pytest.mark.parametrize(
    ("provider", "limit_field"),
    [("local", "max_tokens"), ("openai", "max_completion_tokens")],
)
async def test_gateway_normalizes_one_required_tool_call(provider, limit_field):
    raw_tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="prepare_search_draft", arguments='{"query":"x"}'),
    )
    factory = FakeClientFactory(tool_calls=[[raw_tool_call]])
    config = resolve_ai_gateway_config(settings(
        ai_provider=provider,
        ai_api_key="test-key" if provider == "openai" else "",
    ))
    gateway = build_ai_gateway(config, client_factory=factory)

    call = await gateway.next_tool_call(
        instructions="Use one tool.",
        messages=[{"role": "user", "content": "Find problems"}],
        tools=[{"type": "function", "function": {"name": "prepare_search_draft"}}],
        max_output_tokens=1_200,
    )

    assert call.call_id == "call-1"
    assert call.name == "prepare_search_draft"
    assert call.arguments == {"query": "x"}
    request = factory.chat_completions.requests[0]
    assert request["tool_choice"] == "required"
    assert request["parallel_tool_calls"] is False
    assert request[limit_field] == 1_200
    if provider == "openai":
        assert request["reasoning_effort"] == "medium"


@pytest.mark.parametrize(
    ("error", "category"),
    [(asyncio.TimeoutError(), "timeout"), (ConnectionError("secret"), "connection")],
)
async def test_gateway_sanitizes_transport_failures(error, category):
    factory = FakeClientFactory(error=error)
    gateway = build_ai_gateway(
        resolve_ai_gateway_config(settings()),
        client_factory=factory,
    )

    with pytest.raises(AIGatewayFailure) as failure:
        await gateway.generate_structured(
            output_model=ExampleOutput,
            schema_name="example_output",
            instructions="Return the contract.",
            payload={},
            max_output_tokens=1_000,
        )

    assert failure.value.category == category
    assert str(failure.value) == category
