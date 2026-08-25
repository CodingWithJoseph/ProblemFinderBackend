"""Single schema-constrained AI gateway for every Immensity workflow."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlparse

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings


AIProviderName = Literal["ollama", "openai"]
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
OLLAMA_DEFAULT_MODEL = "gpt-oss:20b"
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-5.6-terra"
STRUCTURED_OUTPUT_ATTEMPTS = 2
TOOL_CALL_ATTEMPTS = 2
VALID_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}


class AIGatewayFailure(RuntimeError):
    """A gateway failure represented only by a safe, fixed category."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class AIGatewayConfig:
    name: AIProviderName
    base_url: str
    model: str
    timeout_seconds: float
    reasoning_effort: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class AIToolCall:
    """One normalized function call returned by the configured model."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    assistant_message: dict[str, Any]


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
    """Inline local references for runtimes that cannot resolve ``$defs``."""

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


def resolve_ai_gateway_config(settings: Settings) -> AIGatewayConfig:
    provider = settings.ai_provider.strip().lower()
    if provider not in {"ollama", "openai"}:
        raise AIGatewayFailure("invalid_provider")
    if settings.environment.strip().lower() in {"prod", "production"} and provider != "openai":
        raise AIGatewayFailure("invalid_configuration")

    timeout = max(5.0, min(float(settings.ai_timeout_seconds), 300.0))
    reasoning_effort = settings.ai_reasoning_effort.strip().lower() or "medium"
    if reasoning_effort not in VALID_REASONING_EFFORTS:
        raise AIGatewayFailure("invalid_configuration")

    if provider == "ollama":
        return AIGatewayConfig(
            name="ollama",
            base_url=_valid_base_url(settings.ai_base_url or OLLAMA_BASE_URL),
            model=settings.ai_model.strip() or OLLAMA_DEFAULT_MODEL,
            timeout_seconds=timeout,
            reasoning_effort=reasoning_effort,
            api_key="ollama-not-a-secret",
        )

    api_key = settings.ai_api_key.strip()
    if not api_key:
        raise AIGatewayFailure("missing_credentials")
    base_url = _valid_base_url(settings.ai_base_url or OPENAI_BASE_URL)
    if base_url != OPENAI_BASE_URL:
        # Never send an OpenAI credential to an arbitrary host.
        raise AIGatewayFailure("invalid_configuration")
    return AIGatewayConfig(
        name="openai",
        base_url=base_url,
        model=settings.ai_model.strip() or OPENAI_DEFAULT_MODEL,
        timeout_seconds=timeout,
        reasoning_effort=reasoning_effort,
        api_key=api_key,
    )


class AIGateway:
    """Provider-neutral model operations used by Search, Analyze, and Build."""

    def __init__(self, config: AIGatewayConfig, client: Any):
        self.config = config
        self.name = config.name
        self.model = config.model
        self._client = client

    async def generate_structured(
        self,
        *,
        output_model: type[BaseModel],
        schema_name: str,
        instructions: str,
        payload: dict[str, Any],
        max_output_tokens: int,
    ) -> BaseModel:
        """Generate and validate one operation-specific JSON contract."""

        if self.name == "openai":
            return await self._generate_with_responses(
                output_model=output_model,
                schema_name=schema_name,
                instructions=instructions,
                payload=payload,
                max_output_tokens=max_output_tokens,
            )
        return await self._generate_with_chat(
            output_model=output_model,
            schema_name=schema_name,
            instructions=instructions,
            payload=payload,
            max_output_tokens=max_output_tokens,
        )

    async def next_tool_call(
        self,
        *,
        instructions: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_output_tokens: int,
    ) -> AIToolCall:
        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": instructions}, *messages],
            "tools": tools,
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "temperature": 0,
            "stream": False,
        }
        if self.name == "openai":
            request["reasoning_effort"] = self.config.reasoning_effort
            request["max_completion_tokens"] = max_output_tokens
        else:
            request["max_tokens"] = max_output_tokens

        failure = AIGatewayFailure("invalid_response")
        for attempt in range(TOOL_CALL_ATTEMPTS):
            response = await self._chat_completion(request)
            try:
                message = response.choices[0].message
                if getattr(message, "refusal", None):
                    raise AIGatewayFailure("refusal")
                tool_calls = message.tool_calls
                if not isinstance(tool_calls, list) or len(tool_calls) != 1:
                    raise AIGatewayFailure("invalid_response")
                tool_call = tool_calls[0]
                call_id = str(tool_call.id).strip()
                name = str(tool_call.function.name).strip()
                raw_arguments = tool_call.function.arguments
                if not call_id or len(call_id) > 200 or not name or len(name) > 100:
                    raise AIGatewayFailure("invalid_response")
                if isinstance(raw_arguments, str):
                    arguments = json.loads(raw_arguments)
                    serialized_arguments = raw_arguments
                elif isinstance(raw_arguments, dict):
                    arguments = raw_arguments
                    serialized_arguments = json.dumps(raw_arguments, ensure_ascii=False)
                else:
                    raise AIGatewayFailure("invalid_response")
                if not isinstance(arguments, dict):
                    raise AIGatewayFailure("invalid_response")
            except json.JSONDecodeError:
                failure = AIGatewayFailure("invalid_json")
            except (AttributeError, IndexError, TypeError):
                failure = AIGatewayFailure("invalid_response")
            except AIGatewayFailure as exc:
                failure = exc
            else:
                return AIToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    assistant_message={
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [{
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": serialized_arguments},
                        }],
                    },
                )

            if failure.category not in {"invalid_json", "invalid_response"}:
                raise failure
            if attempt + 1 >= TOOL_CALL_ATTEMPTS:
                raise failure
        raise failure

    async def _generate_with_chat(
        self,
        *,
        output_model: type[BaseModel],
        schema_name: str,
        instructions: str,
        payload: dict[str, Any],
        max_output_tokens: int,
    ) -> BaseModel:
        schema = inline_local_schema_refs(strict_json_schema(output_model))
        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        last_failure = "invalid_response"
        for attempt in range(STRUCTURED_OUTPUT_ATTEMPTS):
            request = {
                "model": self.config.model,
                "messages": messages,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    },
                },
                "temperature": 0,
                "stream": False,
                "max_tokens": max_output_tokens,
            }
            content = ""
            validation_summary: list[dict[str, str]] = []
            response = await self._chat_completion(request)
            try:
                message = response.choices[0].message
                if getattr(message, "refusal", None):
                    raise AIGatewayFailure("refusal")
                content = message.content
                if not isinstance(content, str) or not content.strip():
                    raise AIGatewayFailure("empty_response")
                return output_model.model_validate(json.loads(content))
            except AIGatewayFailure:
                raise
            except json.JSONDecodeError:
                last_failure = "invalid_json"
            except ValidationError as exc:
                last_failure = "schema_invalid"
                validation_summary = _validation_summary(exc)
            except (AttributeError, IndexError, TypeError):
                last_failure = "invalid_response"

            if attempt + 1 < STRUCTURED_OUTPUT_ATTEMPTS:
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": json.dumps({
                        "repair": "Return a complete corrected object matching the schema.",
                        "validationIssues": validation_summary or [{"message": last_failure}],
                    })},
                ]
        raise AIGatewayFailure(last_failure)

    async def _generate_with_responses(
        self,
        *,
        output_model: type[BaseModel],
        schema_name: str,
        instructions: str,
        payload: dict[str, Any],
        max_output_tokens: int,
    ) -> BaseModel:
        schema = strict_json_schema(output_model)
        last_failure = "invalid_response"
        previous_output = ""
        validation_summary: list[dict[str, str]] = []
        for attempt in range(STRUCTURED_OUTPUT_ATTEMPTS):
            request_payload = payload
            if attempt:
                request_payload = {
                    **payload,
                    "repair": {
                        "instruction": "Return a complete corrected object matching the schema.",
                        "validationIssues": validation_summary or [{"message": last_failure}],
                        "previousOutput": previous_output,
                    },
                }
            request = {
                "model": self.config.model,
                "instructions": instructions,
                "input": json.dumps(request_payload, ensure_ascii=False),
                "text": {"format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }, "verbosity": "low"},
                "reasoning": {"effort": self.config.reasoning_effort},
                "max_output_tokens": max_output_tokens,
                "store": False,
            }
            response = await self._responses_completion(request)
            try:
                previous_output = response.output_text
                if not isinstance(previous_output, str) or not previous_output.strip():
                    if _response_was_refused(response):
                        raise AIGatewayFailure("refusal")
                    raise AIGatewayFailure("empty_response")
                return output_model.model_validate(json.loads(previous_output))
            except AIGatewayFailure:
                raise
            except json.JSONDecodeError:
                last_failure = "invalid_json"
                validation_summary = []
            except ValidationError as exc:
                last_failure = "schema_invalid"
                validation_summary = _validation_summary(exc)
            except (AttributeError, TypeError):
                last_failure = "invalid_response"
                validation_summary = []
        raise AIGatewayFailure(last_failure)

    async def _chat_completion(self, request: dict[str, Any]) -> Any:
        try:
            return await asyncio.wait_for(
                self._client.chat.completions.create(**request),
                timeout=self.config.timeout_seconds + 0.5,
            )
        except (asyncio.TimeoutError, APITimeoutError):
            raise AIGatewayFailure("timeout") from None
        except (APIConnectionError, ConnectionError):
            raise AIGatewayFailure("connection") from None
        except AIGatewayFailure:
            raise
        except Exception:
            raise AIGatewayFailure("provider_error") from None

    async def _responses_completion(self, request: dict[str, Any]) -> Any:
        try:
            return await asyncio.wait_for(
                self._client.responses.create(**request),
                timeout=self.config.timeout_seconds + 0.5,
            )
        except (asyncio.TimeoutError, APITimeoutError):
            raise AIGatewayFailure("timeout") from None
        except (APIConnectionError, ConnectionError):
            raise AIGatewayFailure("connection") from None
        except AIGatewayFailure:
            raise
        except Exception:
            raise AIGatewayFailure("provider_error") from None


def build_ai_gateway(
    config: AIGatewayConfig,
    *,
    client_factory: Any = AsyncOpenAI,
) -> AIGateway:
    try:
        client = client_factory(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
    except Exception:
        raise AIGatewayFailure("invalid_configuration") from None
    return AIGateway(config, client)


@lru_cache(maxsize=4)
def _cached_gateway(config: AIGatewayConfig) -> AIGateway:
    return build_ai_gateway(config)


def get_ai_gateway(settings: Settings) -> AIGateway:
    return _cached_gateway(resolve_ai_gateway_config(settings))


def _valid_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AIGatewayFailure("invalid_configuration")
    return normalized


def _validation_summary(exc: ValidationError) -> list[dict[str, str]]:
    return [{
        "path": ".".join(str(part) for part in issue["loc"]),
        "message": issue["msg"],
    } for issue in exc.errors()[:12]]


def _response_was_refused(response: Any) -> bool:
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            if getattr(content, "type", None) == "refusal":
                return True
    return False
