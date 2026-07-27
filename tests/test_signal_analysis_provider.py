import json
from types import SimpleNamespace

import pytest

from app.services.signal_analysis_provider import (
    GROQ_BASE_URL,
    SignalAnalysisProviderFailure,
    build_signal_analysis_provider,
    resolve_signal_analysis_provider_config,
)


class SequencedCompletions:
    def __init__(self, contents):
        self.contents = list(contents)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        content = self.contents.pop(0)
        return SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=content, refusal=None))
        ])


class FakeClientFactory:
    def __init__(self, contents):
        self.completions = SequencedCompletions(contents)
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=self.completions)
        )
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.client


def settings(**overrides):
    values = {
        "signal_analysis_provider": "",
        "signal_analysis_base_url": "",
        "signal_analysis_model": "",
        "signal_analysis_timeout_seconds": 90.0,
        "search_interpreter_provider": "local",
        "search_interpreter_base_url": "http://127.0.0.1:8080/v1",
        "search_interpreter_model": "gpt-oss-20b",
        "groq_api_key": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def valid_output() -> dict:
    return {
        "thesis": None,
        "claims": [{
            "id": "claim-1",
            "text": "Invoice follow-up is manual.",
            "kind": "observed",
            "confidence": "medium",
            "evidenceIds": ["evidence-1"],
            "confirmed": False,
            "rejected": False,
        }],
        "problemUnits": [],
        "audiences": [],
        "alternatives": [],
        "assumptions": [],
        "recommendedFocus": None,
    }


def test_signal_provider_inherits_local_search_configuration():
    config = resolve_signal_analysis_provider_config(settings())

    assert config.name == "local"
    assert config.base_url == "http://127.0.0.1:8080/v1"
    assert config.model == "gpt-oss-20b"


def test_signal_provider_never_sends_groq_key_to_custom_host():
    with pytest.raises(SignalAnalysisProviderFailure) as exc:
        resolve_signal_analysis_provider_config(settings(
            signal_analysis_provider="groq",
            signal_analysis_base_url="https://example.com/v1",
            groq_api_key="secret",
        ))

    assert exc.value.category == "invalid_configuration"


async def test_local_provider_uses_strict_schema_and_repairs_once():
    factory = FakeClientFactory([
        "{}",
        json.dumps(valid_output()),
    ])
    provider = build_signal_analysis_provider(
        resolve_signal_analysis_provider_config(settings()),
        client_factory=factory,
    )

    result = await provider.generate(
        instructions="Analyze",
        payload={"evidence": [{"id": "evidence-1"}]},
    )

    assert result.claims[0].evidence_ids == ["evidence-1"]
    assert len(factory.completions.requests) == 2
    request = factory.completions.requests[0]
    assert request["max_tokens"] == 8_000
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


async def test_conversation_uses_smaller_structured_completion():
    factory = FakeClientFactory([
        json.dumps({
            "text": "The evidence is limited.",
            "citations": [],
            "proposal": None,
            "insufficientEvidence": True,
        }),
    ])
    provider = build_signal_analysis_provider(
        resolve_signal_analysis_provider_config(settings()),
        client_factory=factory,
    )

    result = await provider.ask(
        instructions="Answer from evidence",
        payload={"case": {}},
    )

    assert result.insufficient_evidence is True
    assert factory.completions.requests[0]["max_tokens"] == 2_500
    assert (
        factory.completions.requests[0]["response_format"]["json_schema"]["name"]
        == "signal_conversation"
    )


def test_groq_provider_defaults_to_official_endpoint():
    config = resolve_signal_analysis_provider_config(settings(
        signal_analysis_provider="groq",
        search_interpreter_base_url="",
        search_interpreter_model="",
        groq_api_key="secret",
    ))

    assert config.base_url == GROQ_BASE_URL
    assert config.api_key == "secret"
