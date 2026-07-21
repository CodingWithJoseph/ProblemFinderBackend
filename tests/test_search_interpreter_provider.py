import asyncio
from types import SimpleNamespace

import pytest

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


def settings(**overrides):
    values = {
        "search_interpreter_provider": "local",
        "search_interpreter_base_url": "",
        "search_interpreter_model": "",
        "search_interpreter_timeout_seconds": 8.0,
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
        "timeout": 8.0,
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
    assert request["max_tokens"] == 1_200
    assert "max_completion_tokens" not in request
    assert "tools" not in request
    assert "user" not in request
    assert "store" not in request
    response_format = request["response_format"]
    assert response_format["type"] == "json_schema"
    assert "json_schema" not in response_format
    assert_strict_objects(response_format["schema"])


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
