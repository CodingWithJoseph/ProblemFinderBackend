"""Provider-neutral, schema-constrained boundary for Build draft generation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI
from pydantic import ValidationError

from app.config import Settings
from app.services.search_interpreter_provider import (
    GROQ_BASE_URL,
    GROQ_DEFAULT_MODEL,
    LOCAL_DEFAULT_BASE_URL,
    LOCAL_DEFAULT_MODEL,
    inline_local_schema_refs,
    strict_json_schema,
)
from app.services.signal_analysis_provider import SignalAnalysisProviderFailure
from app.solution_contract import SolutionModelOutput


SolutionProviderName = Literal["openai", "ollama", "local", "groq"]
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-5.6-terra"
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
OLLAMA_DEFAULT_MODEL = "gpt-oss:20b"
SOLUTION_REPAIR_ATTEMPTS = 2
VALID_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}


@dataclass(frozen=True)
class SolutionProviderConfig:
    name: SolutionProviderName
    base_url: str
    model: str
    timeout_seconds: float
    max_output_tokens: int
    reasoning_effort: str
    api_key: str = field(repr=False)


class SolutionProvider(Protocol):
    name: str
    model: str

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SolutionModelOutput: ...


def resolve_solution_provider_config(settings: Settings) -> SolutionProviderConfig:
    provider = settings.solution_provider.strip().lower() or "ollama"
    timeout = max(5.0, min(float(settings.solution_timeout_seconds), 300.0))
    max_output_tokens = max(2_000, min(int(settings.solution_max_output_tokens), 20_000))
    reasoning_effort = settings.solution_reasoning_effort.strip().lower() or "medium"
    if reasoning_effort not in VALID_REASONING_EFFORTS:
        raise SignalAnalysisProviderFailure("invalid_configuration")

    configured_base_url = settings.solution_base_url.strip().rstrip("/")
    configured_model = settings.solution_model.strip()

    if provider == "openai":
        api_key = settings.openai_api_key.strip()
        if not api_key:
            raise SignalAnalysisProviderFailure("missing_credentials")
        base_url = _valid_base_url(configured_base_url or OPENAI_BASE_URL)
        if base_url != OPENAI_BASE_URL:
            raise SignalAnalysisProviderFailure("invalid_configuration")
        return SolutionProviderConfig(
            name="openai",
            base_url=base_url,
            model=configured_model or OPENAI_DEFAULT_MODEL,
            timeout_seconds=timeout,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            api_key=api_key,
        )

    if provider == "ollama":
        return SolutionProviderConfig(
            name="ollama",
            base_url=_valid_base_url(configured_base_url or OLLAMA_BASE_URL),
            model=configured_model or OLLAMA_DEFAULT_MODEL,
            timeout_seconds=timeout,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            api_key="ollama",
        )

    if provider == "local":
        return SolutionProviderConfig(
            name="local",
            base_url=_valid_base_url(configured_base_url or LOCAL_DEFAULT_BASE_URL),
            model=configured_model or LOCAL_DEFAULT_MODEL,
            timeout_seconds=timeout,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            api_key="local-not-a-secret",
        )

    if provider == "groq":
        api_key = settings.groq_api_key.strip()
        if not api_key:
            raise SignalAnalysisProviderFailure("missing_credentials")
        base_url = _valid_base_url(configured_base_url or GROQ_BASE_URL)
        if base_url != GROQ_BASE_URL:
            raise SignalAnalysisProviderFailure("invalid_configuration")
        return SolutionProviderConfig(
            name="groq",
            base_url=base_url,
            model=configured_model or GROQ_DEFAULT_MODEL,
            timeout_seconds=timeout,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            api_key=api_key,
        )

    raise SignalAnalysisProviderFailure("invalid_provider")


class OpenAISolutionProvider:
    """Official OpenAI Responses API implementation for production Build drafts."""

    def __init__(self, config: SolutionProviderConfig, client: Any):
        self.config = config
        self.name = config.name
        self.model = config.model
        self._client = client

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SolutionModelOutput:
        schema = strict_json_schema(SolutionModelOutput)
        last_failure = "invalid_response"
        previous_output = ""
        validation_summary: list[dict[str, str]] = []

        for attempt in range(SOLUTION_REPAIR_ATTEMPTS):
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
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "solution_case",
                        "strict": True,
                        "schema": schema,
                    },
                    "verbosity": "low",
                },
                "reasoning": {"effort": self.config.reasoning_effort},
                "max_output_tokens": self.config.max_output_tokens,
                "store": False,
            }
            try:
                response = await asyncio.wait_for(
                    self._client.responses.create(**request),
                    timeout=self.config.timeout_seconds + 0.5,
                )
            except (asyncio.TimeoutError, APITimeoutError):
                raise SignalAnalysisProviderFailure("timeout") from None
            except (APIConnectionError, ConnectionError):
                raise SignalAnalysisProviderFailure("connection") from None
            except Exception:
                raise SignalAnalysisProviderFailure("provider_error") from None

            try:
                previous_output = response.output_text
                if not isinstance(previous_output, str) or not previous_output.strip():
                    if _response_was_refused(response):
                        raise SignalAnalysisProviderFailure("refusal")
                    raise SignalAnalysisProviderFailure("empty_response")
                return SolutionModelOutput.model_validate(json.loads(previous_output))
            except SignalAnalysisProviderFailure:
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

        raise SignalAnalysisProviderFailure(last_failure)


class OpenAICompatibleSolutionProvider:
    """Chat Completions compatibility path for local and Groq development."""

    def __init__(self, config: SolutionProviderConfig, client: Any):
        self.config = config
        self.name = config.name
        self.model = config.model
        self._client = client

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SolutionModelOutput:
        schema = strict_json_schema(SolutionModelOutput)
        if self.name in {"local", "ollama"}:
            schema = inline_local_schema_refs(schema)
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "solution_case",
                "strict": True,
                "schema": schema,
            },
        }
        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        last_failure = "invalid_response"

        for attempt in range(SOLUTION_REPAIR_ATTEMPTS):
            request = {
                "model": self.config.model,
                "messages": messages,
                "response_format": response_format,
                "temperature": 0,
                "stream": False,
                (
                    "max_tokens" if self.name in {"local", "ollama"} else "max_completion_tokens"
                ): self.config.max_output_tokens,
            }
            if self.name == "ollama":
                request["reasoning_effort"] = self.config.reasoning_effort
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
                return SolutionModelOutput.model_validate(json.loads(content))
            except SignalAnalysisProviderFailure:
                raise
            except json.JSONDecodeError:
                last_failure = "invalid_json"
            except ValidationError as exc:
                last_failure = "schema_invalid"
                validation_summary = _validation_summary(exc)
            except (AttributeError, IndexError, TypeError):
                last_failure = "invalid_response"

            if attempt + 1 < SOLUTION_REPAIR_ATTEMPTS:
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": json.dumps({
                            "repair": "Return a complete corrected object matching the schema.",
                            "validationIssues": validation_summary or [{"message": last_failure}],
                        }),
                    },
                ]

        raise SignalAnalysisProviderFailure(last_failure)


def build_solution_provider(
    config: SolutionProviderConfig,
    *,
    client_factory: Any = AsyncOpenAI,
) -> SolutionProvider:
    try:
        client = client_factory(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
    except Exception:
        raise SignalAnalysisProviderFailure("invalid_configuration") from None
    if config.name == "openai":
        return OpenAISolutionProvider(config, client)
    return OpenAICompatibleSolutionProvider(config, client)


@lru_cache(maxsize=4)
def _cached_provider(config: SolutionProviderConfig) -> SolutionProvider:
    return build_solution_provider(config)


def get_solution_provider(settings: Settings) -> SolutionProvider:
    return _cached_provider(resolve_solution_provider_config(settings))


def _valid_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SignalAnalysisProviderFailure("invalid_configuration")
    return normalized


def _validation_summary(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "path": ".".join(str(part) for part in issue["loc"]),
            "message": issue["msg"],
        }
        for issue in exc.errors()[:12]
    ]


def _response_was_refused(response: Any) -> bool:
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            if getattr(content, "type", None) == "refusal":
                return True
    return False
