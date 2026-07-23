"""Provider boundary for schema-constrained conversational search output."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.search_contract import SearchInterpretationModelOutput

LOCAL_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
LOCAL_DEFAULT_MODEL = "gpt-oss-20b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-20b"
SCHEMA_NAME = "search_interpretation"
MAX_COMPLETION_TOKENS = 1_200
LOCAL_MAX_COMPLETION_TOKENS = 2_400
MALFORMED_RESPONSE_ATTEMPTS = 2

ProviderName = Literal["local", "groq"]


class SearchInterpreterProviderFailure(RuntimeError):
    """A provider failure represented only by a safe, fixed category."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class SearchInterpreterProviderConfig:
    name: ProviderName
    base_url: str
    model: str
    timeout_seconds: float
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class SearchAgentToolCall:
    """One normalized, provider-independent function call."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    assistant_message: dict[str, Any]


class SearchInterpreterProvider(Protocol):
    name: ProviderName

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


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Close every object and require every field for strict structured output."""
    schema = model.model_json_schema()

    def make_strict(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object" or "properties" in node:
                properties = node.get("properties", {})
                node["additionalProperties"] = False
                node["required"] = list(properties)
            for value in list(node.values()):
                make_strict(value)
        elif isinstance(node, list):
            for value in node:
                make_strict(value)

    make_strict(schema)
    return schema


def inline_local_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local JSON Schema references for llama.cpp grammar conversion."""
    source = copy.deepcopy(schema)

    def resolve_pointer(pointer: str) -> Any:
        if not pointer.startswith("#/"):
            raise ValueError("Only local JSON Schema references are supported")
        value: Any = source
        for raw_part in pointer[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(value, dict) or part not in value:
                raise ValueError("Unresolvable local JSON Schema reference")
            value = value[part]
        return value

    def inline(node: Any, active_refs: frozenset[str] = frozenset()) -> Any:
        if isinstance(node, list):
            return [inline(value, active_refs) for value in node]
        if not isinstance(node, dict):
            return node

        reference = node.get("$ref")
        if isinstance(reference, str):
            if reference in active_refs:
                raise ValueError("Recursive JSON Schema references are not supported")
            resolved = copy.deepcopy(resolve_pointer(reference))
            siblings = {key: value for key, value in node.items() if key != "$ref"}
            if not isinstance(resolved, dict):
                raise ValueError("JSON Schema reference must resolve to an object")
            resolved.update(siblings)
            return inline(resolved, active_refs | {reference})

        return {
            key: inline(value, active_refs)
            for key, value in node.items()
            if key not in {"$defs", "definitions"}
        }

    inlined = inline(source)
    if not isinstance(inlined, dict):
        raise ValueError("JSON Schema root must be an object")
    return inlined


def _base_url(value: str, default: str) -> str:
    candidate = (value.strip() or default).rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SearchInterpreterProviderFailure("invalid_configuration")
    return candidate


def resolve_search_interpreter_provider_config(
    settings: Settings,
) -> SearchInterpreterProviderConfig:
    provider = settings.search_interpreter_provider.strip().lower()
    timeout_seconds = max(1.0, min(float(settings.search_interpreter_timeout_seconds), 30.0))

    if provider == "local":
        return SearchInterpreterProviderConfig(
            name="local",
            base_url=_base_url(settings.search_interpreter_base_url, LOCAL_DEFAULT_BASE_URL),
            model=settings.search_interpreter_model.strip() or LOCAL_DEFAULT_MODEL,
            timeout_seconds=timeout_seconds,
            api_key="local-not-a-secret",
        )

    if provider == "groq":
        api_key = settings.groq_api_key.strip()
        if not api_key:
            raise SearchInterpreterProviderFailure("missing_credentials")
        base_url = _base_url(settings.search_interpreter_base_url, GROQ_BASE_URL)
        if base_url != GROQ_BASE_URL:
            # Never risk sending a Groq credential to an arbitrary host.
            raise SearchInterpreterProviderFailure("invalid_configuration")
        return SearchInterpreterProviderConfig(
            name="groq",
            base_url=base_url,
            model=settings.search_interpreter_model.strip() or GROQ_DEFAULT_MODEL,
            timeout_seconds=timeout_seconds,
            api_key=api_key,
        )

    raise SearchInterpreterProviderFailure("invalid_provider")


class _OpenAICompatibleChatProvider:
    name: ProviderName

    def __init__(self, config: SearchInterpreterProviderConfig, client: Any):
        self.config = config
        self.name = config.name
        self._client = client

    def _response_format(self, schema: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _completion_limit(self) -> dict[str, int]:
        raise NotImplementedError

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SearchInterpretationModelOutput:
        schema = strict_json_schema(SearchInterpretationModelOutput)
        request = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "response_format": self._response_format(schema),
            "stream": False,
            **self._completion_limit(),
        }

        try:
            response = await asyncio.wait_for(
                self._client.chat.completions.create(**request),
                timeout=self.config.timeout_seconds + 0.5,
            )
        except (asyncio.TimeoutError, APITimeoutError):
            raise SearchInterpreterProviderFailure("timeout") from None
        except (APIConnectionError, ConnectionError):
            raise SearchInterpreterProviderFailure("connection") from None
        except Exception:
            raise SearchInterpreterProviderFailure("provider_error") from None

        try:
            message = response.choices[0].message
            if getattr(message, "refusal", None):
                raise SearchInterpreterProviderFailure("refusal")
            content = message.content
            if not isinstance(content, str) or not content.strip():
                raise SearchInterpreterProviderFailure("empty_response")
            parsed = json.loads(content)
        except SearchInterpreterProviderFailure:
            raise
        except (AttributeError, IndexError, TypeError):
            raise SearchInterpreterProviderFailure("invalid_response") from None
        except json.JSONDecodeError:
            raise SearchInterpreterProviderFailure("invalid_json") from None

        try:
            return SearchInterpretationModelOutput.model_validate(parsed)
        except ValidationError:
            raise SearchInterpreterProviderFailure("schema_invalid") from None

    async def next_agent_tool_call(
        self,
        *,
        instructions: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> SearchAgentToolCall:
        request = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": instructions},
                *messages,
            ],
            "tools": tools,
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "temperature": 0,
            "reasoning_effort": "low",
            "stream": False,
            **self._completion_limit(),
        }

        for attempt in range(MALFORMED_RESPONSE_ATTEMPTS):
            try:
                response = await asyncio.wait_for(
                    self._client.chat.completions.create(**request),
                    timeout=self.config.timeout_seconds + 0.5,
                )
            except (asyncio.TimeoutError, APITimeoutError):
                raise SearchInterpreterProviderFailure("timeout") from None
            except (APIConnectionError, ConnectionError):
                raise SearchInterpreterProviderFailure("connection") from None
            except Exception:
                raise SearchInterpreterProviderFailure("provider_error") from None

            try:
                message = response.choices[0].message
                if getattr(message, "refusal", None):
                    raise SearchInterpreterProviderFailure("refusal")
                tool_calls = message.tool_calls
                if not isinstance(tool_calls, list) or len(tool_calls) != 1:
                    raise SearchInterpreterProviderFailure("invalid_response")

                tool_call = tool_calls[0]
                call_id = str(tool_call.id).strip()
                name = str(tool_call.function.name).strip()
                raw_arguments = tool_call.function.arguments
                if not call_id or len(call_id) > 200 or not name or len(name) > 100:
                    raise SearchInterpreterProviderFailure("invalid_response")
                if isinstance(raw_arguments, str):
                    arguments = json.loads(raw_arguments)
                    serialized_arguments = raw_arguments
                elif isinstance(raw_arguments, dict):
                    arguments = raw_arguments
                    serialized_arguments = json.dumps(raw_arguments, ensure_ascii=False)
                else:
                    raise SearchInterpreterProviderFailure("invalid_response")
                if not isinstance(arguments, dict):
                    raise SearchInterpreterProviderFailure("invalid_response")
            except json.JSONDecodeError:
                failure = SearchInterpreterProviderFailure("invalid_json")
            except (AttributeError, IndexError, TypeError):
                failure = SearchInterpreterProviderFailure("invalid_response")
            except SearchInterpreterProviderFailure as exc:
                failure = exc
            else:
                return SearchAgentToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    assistant_message={
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": serialized_arguments,
                                },
                            }
                        ],
                    },
                )

            if failure.category not in {"invalid_json", "invalid_response"}:
                raise failure
            if attempt + 1 >= MALFORMED_RESPONSE_ATTEMPTS:
                raise failure

        raise SearchInterpreterProviderFailure("invalid_response")


class LocalSearchInterpreterProvider(_OpenAICompatibleChatProvider):
    """llama-server-compatible schema-constrained Chat Completions adapter."""

    name: ProviderName = "local"

    def _response_format(self, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": SCHEMA_NAME,
                "strict": True,
                "schema": inline_local_schema_refs(schema),
            },
        }

    def _completion_limit(self) -> dict[str, int]:
        return {"max_tokens": LOCAL_MAX_COMPLETION_TOKENS}


class GroqSearchInterpreterProvider(_OpenAICompatibleChatProvider):
    """Groq strict JSON Schema Chat Completions adapter."""

    name: ProviderName = "groq"

    def _response_format(self, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": SCHEMA_NAME,
                "strict": True,
                "schema": schema,
            },
        }

    def _completion_limit(self) -> dict[str, int]:
        return {"max_completion_tokens": MAX_COMPLETION_TOKENS}


def build_search_interpreter_provider(
    config: SearchInterpreterProviderConfig,
    *,
    client_factory: Any = AsyncOpenAI,
) -> SearchInterpreterProvider:
    try:
        client = client_factory(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
    except Exception:
        raise SearchInterpreterProviderFailure("invalid_configuration") from None

    if config.name == "local":
        return LocalSearchInterpreterProvider(config, client)
    return GroqSearchInterpreterProvider(config, client)


@lru_cache(maxsize=4)
def _cached_provider(config: SearchInterpreterProviderConfig) -> SearchInterpreterProvider:
    return build_search_interpreter_provider(config)


def get_search_interpreter_provider(settings: Settings) -> SearchInterpreterProvider:
    """Resolve and lazily initialize the configured optional provider."""
    return _cached_provider(resolve_search_interpreter_provider_config(settings))
