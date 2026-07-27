"""Provider-neutral, schema-constrained Signal analysis model boundary."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.services.search_interpreter_provider import (
    GROQ_BASE_URL,
    GROQ_DEFAULT_MODEL,
    LOCAL_DEFAULT_BASE_URL,
    LOCAL_DEFAULT_MODEL,
    inline_local_schema_refs,
    strict_json_schema,
)
from app.signal_contract import SignalAnalysisModelOutput, SignalConversationModelOutput


ProviderName = Literal["local", "groq"]
SIGNAL_SCHEMA_NAME = "signal_analysis"
SIGNAL_MAX_COMPLETION_TOKENS = 8_000
SIGNAL_REPAIR_ATTEMPTS = 2


class SignalAnalysisProviderFailure(RuntimeError):
    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class SignalAnalysisProviderConfig:
    name: ProviderName
    base_url: str
    model: str
    timeout_seconds: float
    api_key: str = field(repr=False)


class SignalAnalysisProvider(Protocol):
    name: ProviderName
    model: str

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SignalAnalysisModelOutput: ...

    async def ask(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SignalConversationModelOutput: ...


def resolve_signal_analysis_provider_config(settings: Settings) -> SignalAnalysisProviderConfig:
    provider = (
        settings.signal_analysis_provider.strip()
        or settings.search_interpreter_provider.strip()
        or "local"
    ).lower()
    timeout = max(5.0, min(float(settings.signal_analysis_timeout_seconds), 180.0))
    configured_base_url = (
        settings.signal_analysis_base_url.strip()
        or settings.search_interpreter_base_url.strip()
    )
    configured_model = (
        settings.signal_analysis_model.strip()
        or settings.search_interpreter_model.strip()
    )

    if provider == "local":
        return SignalAnalysisProviderConfig(
            name="local",
            base_url=_valid_base_url(configured_base_url or LOCAL_DEFAULT_BASE_URL),
            model=configured_model or LOCAL_DEFAULT_MODEL,
            timeout_seconds=timeout,
            api_key="local-not-a-secret",
        )

    if provider == "groq":
        api_key = settings.groq_api_key.strip()
        if not api_key:
            raise SignalAnalysisProviderFailure("missing_credentials")
        base_url = _valid_base_url(configured_base_url or GROQ_BASE_URL)
        if base_url != GROQ_BASE_URL:
            raise SignalAnalysisProviderFailure("invalid_configuration")
        return SignalAnalysisProviderConfig(
            name="groq",
            base_url=base_url,
            model=configured_model or GROQ_DEFAULT_MODEL,
            timeout_seconds=timeout,
            api_key=api_key,
        )

    raise SignalAnalysisProviderFailure("invalid_provider")


class OpenAICompatibleSignalAnalysisProvider:
    def __init__(self, config: SignalAnalysisProviderConfig, client: Any):
        self.config = config
        self.name = config.name
        self.model = config.model
        self._client = client

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SignalAnalysisModelOutput:
        result = await self._generate_structured(
            output_model=SignalAnalysisModelOutput,
            schema_name=SIGNAL_SCHEMA_NAME,
            instructions=instructions,
            payload=payload,
            max_tokens=SIGNAL_MAX_COMPLETION_TOKENS,
        )
        return SignalAnalysisModelOutput.model_validate(result)

    async def ask(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SignalConversationModelOutput:
        result = await self._generate_structured(
            output_model=SignalConversationModelOutput,
            schema_name="signal_conversation",
            instructions=instructions,
            payload=payload,
            max_tokens=2_500,
        )
        return SignalConversationModelOutput.model_validate(result)

    async def _generate_structured(
        self,
        *,
        output_model: type[BaseModel],
        schema_name: str,
        instructions: str,
        payload: dict[str, Any],
        max_tokens: int,
    ) -> BaseModel:
        schema = strict_json_schema(output_model)
        if self.name == "local":
            schema = inline_local_schema_refs(schema)
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
        }
        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        last_failure = "invalid_response"
        for attempt in range(SIGNAL_REPAIR_ATTEMPTS):
            request = {
                "model": self.config.model,
                "messages": messages,
                "response_format": response_format,
                "temperature": 0,
                "stream": False,
                (
                    "max_tokens"
                    if self.name == "local"
                    else "max_completion_tokens"
                ): max_tokens,
            }
            content = ""
            validation_summary: list[dict[str, str]] = []
            try:
                response = await asyncio.wait_for(
                    self._client.chat.completions.create(**request),
                    timeout=self.config.timeout_seconds + 0.5,
                )
            except (asyncio.TimeoutError, APITimeoutError):
                raise SignalAnalysisProviderFailure("timeout") from None
            except (APIConnectionError, ConnectionError):
                raise SignalAnalysisProviderFailure("connection") from None
            except Exception:
                raise SignalAnalysisProviderFailure("provider_error") from None

            try:
                message = response.choices[0].message
                if getattr(message, "refusal", None):
                    raise SignalAnalysisProviderFailure("refusal")
                content = message.content
                if not isinstance(content, str) or not content.strip():
                    raise SignalAnalysisProviderFailure("empty_response")
                parsed = json.loads(content)
                return output_model.model_validate(parsed)
            except SignalAnalysisProviderFailure:
                raise
            except json.JSONDecodeError:
                last_failure = "invalid_json"
            except ValidationError as exc:
                last_failure = "schema_invalid"
                validation_summary = [
                    {
                        "path": ".".join(str(part) for part in issue["loc"]),
                        "message": issue["msg"],
                    }
                    for issue in exc.errors()[:12]
                ]
            except (AttributeError, IndexError, TypeError):
                last_failure = "invalid_response"

            if attempt + 1 < SIGNAL_REPAIR_ATTEMPTS:
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": json.dumps({
                            "repair": "Return a complete corrected object matching the schema.",
                            "validationIssues": (
                                validation_summary
                                if validation_summary
                                else [{"message": last_failure}]
                            ),
                        }),
                    },
                ]

        raise SignalAnalysisProviderFailure(last_failure)


def build_signal_analysis_provider(
    config: SignalAnalysisProviderConfig,
    *,
    client_factory: Any = AsyncOpenAI,
) -> SignalAnalysisProvider:
    try:
        client = client_factory(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
    except Exception:
        raise SignalAnalysisProviderFailure("invalid_configuration") from None
    return OpenAICompatibleSignalAnalysisProvider(config, client)


@lru_cache(maxsize=4)
def _cached_provider(config: SignalAnalysisProviderConfig) -> SignalAnalysisProvider:
    return build_signal_analysis_provider(config)


def get_signal_analysis_provider(settings: Settings) -> SignalAnalysisProvider:
    return _cached_provider(resolve_signal_analysis_provider_config(settings))


def _valid_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SignalAnalysisProviderFailure("invalid_configuration")
    return normalized
