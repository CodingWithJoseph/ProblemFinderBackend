"""Schema-constrained provider boundary for Solution Case generation."""

from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from typing import Any, Protocol

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI
from pydantic import ValidationError

from app.config import Settings
from app.services.search_interpreter_provider import inline_local_schema_refs, strict_json_schema
from app.services.signal_analysis_provider import (
    SignalAnalysisProviderConfig,
    SignalAnalysisProviderFailure,
    resolve_signal_analysis_provider_config,
)
from app.solution_contract import SolutionModelOutput


SOLUTION_MAX_COMPLETION_TOKENS = 10_000
SOLUTION_REPAIR_ATTEMPTS = 2


class SolutionProvider(Protocol):
    name: str
    model: str

    async def generate(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
    ) -> SolutionModelOutput: ...


class OpenAICompatibleSolutionProvider:
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
    ) -> SolutionModelOutput:
        schema = strict_json_schema(SolutionModelOutput)
        if self.name == "local":
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
                    "max_tokens" if self.name == "local" else "max_completion_tokens"
                ): SOLUTION_MAX_COMPLETION_TOKENS,
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
                return SolutionModelOutput.model_validate(json.loads(content))
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
    config: SignalAnalysisProviderConfig,
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
    return OpenAICompatibleSolutionProvider(config, client)


@lru_cache(maxsize=4)
def _cached_provider(config: SignalAnalysisProviderConfig) -> SolutionProvider:
    return build_solution_provider(config)


def get_solution_provider(settings: Settings) -> SolutionProvider:
    return _cached_provider(resolve_signal_analysis_provider_config(settings))
