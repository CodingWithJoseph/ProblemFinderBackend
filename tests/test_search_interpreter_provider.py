import asyncio
from types import SimpleNamespace

import pytest

from app.prompts.search_interpreter import SEARCH_INTERPRETER_INSTRUCTIONS
from app.search_contract import ClusterSearchQuery, SearchInterpretationModelOutput
from app.services.search_interpreter_provider import (
    GROQ_BASE_URL,
    GROQ_DEFAULT_MODEL,
    LOCAL_DEFAULT_BASE_URL,
    LOCAL_DEFAULT_MODEL,
    SearchInterpreterProviderFailure,
    build_search_interpreter_provider,
    resolve_search_interpreter_provider_config,
)


class FakeCompletions:
    def __init__(self, content=None, error: Exception | None = None):
        self.content = content
        self.error = error
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        message = SimpleNamespace(content=self.content, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, content=None, error: Exception | None = None):
        self.chat = SimpleNamespace(completions=FakeCompletions(content, error))


class FakeClientFactory:
    def __init__(self, content=None, error: Exception | None = None):
        self.client = FakeClient(content, error)
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.client


class FakeToolCompletions:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        message = SimpleNamespace(content=None, refusal=None, tool_calls=self.tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeToolClientFactory:
    def __init__(self, tool_calls):
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeToolCompletions(tool_calls))
        )

    def __call__(self, **kwargs):
        return self.client


class SequencedToolCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        tool_calls = self.responses.pop(0)
        message = SimpleNamespace(content=None, refusal=None, tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class SequencedToolClientFactory:
    def __init__(self, responses):
        self.completions = SequencedToolCompletions(responses)
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=self.completions)
        )

    def __call__(self, **kwargs):
        return self.client


def settings(**overrides):
    values = {
        "search_interpreter_provider": "local",
        "search_interpreter_base_url": "",
        "search_interpreter_model": "",
        "search_interpreter_timeout_seconds": 30.0,
        "groq_api_key": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def valid_output_json():
    output = SearchInterpretationModelOutput(
        draft=ClusterSearchQuery(query="invoice delays"),
        assumptions=[],
        unsupported=[],
        clarification_question=None,
    )
    return output.model_dump_json()


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


def test_interpreter_prompt_requires_neutral_filters_for_new_searches():
    assert "Never invent or tighten a filter" in SEARCH_INTERPRETER_INSTRUCTIONS
    assert "observed_after null" in SEARCH_INTERPRETER_INSTRUCTIONS
    assert "preserve current_draft filters" in SEARCH_INTERPRETER_INSTRUCTIONS


def test_provider_configuration_selects_local_and_groq_defaults():
    local = resolve_search_interpreter_provider_config(settings())
    assert local.name == "local"
    assert local.base_url == LOCAL_DEFAULT_BASE_URL
    assert local.model == LOCAL_DEFAULT_MODEL
    assert local.api_key == "local-not-a-secret"

    groq = resolve_search_interpreter_provider_config(settings(
        search_interpreter_provider=" GROQ ",
        groq_api_key="groq-secret",
    ))
    assert groq.name == "groq"
    assert groq.base_url == GROQ_BASE_URL
    assert groq.model == GROQ_DEFAULT_MODEL
    assert groq.api_key == "groq-secret"


def test_groq_configuration_rejects_missing_credentials_and_non_groq_host():
    with pytest.raises(SearchInterpreterProviderFailure) as missing:
        resolve_search_interpreter_provider_config(settings(
            search_interpreter_provider="groq",
        ))
    assert missing.value.category == "missing_credentials"

    with pytest.raises(SearchInterpreterProviderFailure) as unsafe_host:
        resolve_search_interpreter_provider_config(settings(
            search_interpreter_provider="groq",
            search_interpreter_base_url="https://example.com/v1",
            groq_api_key="groq-secret",
        ))
    assert unsafe_host.value.category == "invalid_configuration"


async def test_groq_uses_strict_json_schema_chat_completions_shape():
    config = resolve_search_interpreter_provider_config(settings(
        search_interpreter_provider="groq",
        search_interpreter_base_url=GROQ_BASE_URL,
        search_interpreter_model="openai/gpt-oss-20b",
        groq_api_key="groq-secret",
    ))
    factory = FakeClientFactory(valid_output_json())
    provider = build_search_interpreter_provider(config, client_factory=factory)

    output = await provider.generate(
        instructions="system instructions",
        payload={"latest_user_message": "invoice problems"},
    )

    assert output.draft.query == "invoice delays"
    assert factory.kwargs == {
        "api_key": "groq-secret",
        "base_url": GROQ_BASE_URL,
        "timeout": 30.0,
        "max_retries": 0,
    }
    request = factory.client.chat.completions.kwargs
    assert request["model"] == "openai/gpt-oss-20b"
    assert request["stream"] is False
    assert request["max_completion_tokens"] == 1_200
    assert "tools" not in request
    assert "user" not in request
    assert "store" not in request  # Groq Chat Completions does not support this field.
    response_format = request["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["name"] == "search_interpretation"
    assert_strict_objects(response_format["json_schema"]["schema"])


async def test_local_uses_llama_server_schema_constrained_shape():
    config = resolve_search_interpreter_provider_config(settings(
        search_interpreter_base_url="http://host.docker.internal:8080/v1",
        search_interpreter_model="my-gpt-oss-alias",
    ))
    factory = FakeClientFactory(valid_output_json())
    provider = build_search_interpreter_provider(config, client_factory=factory)

    output = await provider.generate(
        instructions="system instructions",
        payload={"latest_user_message": "invoice problems"},
    )

    assert output.draft.query == "invoice delays"
    assert factory.kwargs["base_url"] == "http://host.docker.internal:8080/v1"
    assert factory.kwargs["api_key"] == "local-not-a-secret"
    request = factory.client.chat.completions.kwargs
    assert request["model"] == "my-gpt-oss-alias"
    assert request["stream"] is False
    assert request["max_tokens"] == 2_400
    assert "max_completion_tokens" not in request
    assert "tools" not in request
    assert "user" not in request
    assert "store" not in request
    response_format = request["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["name"] == "search_interpretation"
    schema = response_format["json_schema"]["schema"]
    assert "$defs" not in schema
    assert "$ref" not in str(schema)
    assert schema["properties"]["draft"]["type"] == "object"
    assert_strict_objects(schema)


@pytest.mark.parametrize(
    ("provider_name", "expected_limit"),
    [
        ("local", "max_tokens"),
        ("groq", "max_completion_tokens"),
    ],
)
async def test_provider_normalizes_one_required_agent_tool_call(
    provider_name,
    expected_limit,
):
    config = resolve_search_interpreter_provider_config(settings(
        search_interpreter_provider=provider_name,
        search_interpreter_base_url=(
            GROQ_BASE_URL if provider_name == "groq" else LOCAL_DEFAULT_BASE_URL
        ),
        groq_api_key="groq-secret" if provider_name == "groq" else "",
    ))
    raw_tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name="get_search_filter_options",
            arguments="{}",
        ),
    )
    factory = FakeToolClientFactory([raw_tool_call])
    provider = build_search_interpreter_provider(config, client_factory=factory)
    tools = [{
        "type": "function",
        "function": {
            "name": "get_search_filter_options",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    }]

    call = await provider.next_agent_tool_call(
        instructions="Use tools only.",
        messages=[{"role": "user", "content": "search request"}],
        tools=tools,
    )

    assert call.call_id == "call-1"
    assert call.name == "get_search_filter_options"
    assert call.arguments == {}
    request = factory.client.chat.completions.kwargs
    assert request["tool_choice"] == "required"
    assert request["parallel_tool_calls"] is False
    assert request["temperature"] == 0
    assert request["reasoning_effort"] == "low"
    assert request["tools"] == tools
    assert request[expected_limit] == (2_400 if provider_name == "local" else 1_200)
    assert "response_format" not in request
    assert "user" not in request
    assert "store" not in request


@pytest.mark.parametrize(
    ("tool_calls", "category"),
    [
        ([], "invalid_response"),
        ([SimpleNamespace(
            id="call-1",
            function=SimpleNamespace(name="prepare_search_draft", arguments="not-json"),
        )], "invalid_json"),
    ],
)
async def test_provider_rejects_invalid_agent_tool_responses(tool_calls, category):
    config = resolve_search_interpreter_provider_config(settings())
    provider = build_search_interpreter_provider(
        config,
        client_factory=FakeToolClientFactory(tool_calls),
    )

    with pytest.raises(SearchInterpreterProviderFailure) as failure:
        await provider.next_agent_tool_call(
            instructions="Use tools only.",
            messages=[{"role": "user", "content": "search request"}],
            tools=[],
        )

    assert failure.value.category == category


async def test_provider_retries_one_malformed_agent_response():
    valid_call = SimpleNamespace(
        id="call-2",
        function=SimpleNamespace(
            name="get_search_filter_options",
            arguments="{}",
        ),
    )
    factory = SequencedToolClientFactory([[], [valid_call]])
    provider = build_search_interpreter_provider(
        resolve_search_interpreter_provider_config(settings()),
        client_factory=factory,
    )

    call = await provider.next_agent_tool_call(
        instructions="Use tools only.",
        messages=[{"role": "user", "content": "search request"}],
        tools=[],
    )

    assert call.call_id == "call-2"
    assert factory.completions.calls == 2


@pytest.mark.parametrize(
    ("content", "category"),
    [
        ("not json", "invalid_json"),
        ("{}", "schema_invalid"),
    ],
)
async def test_provider_rejects_invalid_output(content, category):
    config = resolve_search_interpreter_provider_config(settings())
    provider = build_search_interpreter_provider(
        config,
        client_factory=FakeClientFactory(content),
    )

    with pytest.raises(SearchInterpreterProviderFailure) as failure:
        await provider.generate(instructions="system", payload={"message": "test"})

    assert failure.value.category == category


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (asyncio.TimeoutError(), "timeout"),
        (ConnectionError("refused"), "connection"),
    ],
)
async def test_provider_sanitizes_timeout_and_connection_failures(error, category):
    config = resolve_search_interpreter_provider_config(settings())
    provider = build_search_interpreter_provider(
        config,
        client_factory=FakeClientFactory(error=error),
    )

    with pytest.raises(SearchInterpreterProviderFailure) as failure:
        await provider.generate(instructions="system", payload={"message": "test"})

    assert failure.value.category == category
    assert str(failure.value) == category
