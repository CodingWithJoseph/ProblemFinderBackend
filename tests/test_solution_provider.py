import json
from types import SimpleNamespace

import pytest

from app.services.signal_analysis_provider import SignalAnalysisProviderFailure
from app.services.solution_provider import (
    OPENAI_BASE_URL,
    OPENAI_DEFAULT_MODEL,
    build_solution_provider,
    resolve_solution_provider_config,
)


class SequencedCompletions:
    def __init__(self, contents):
        self.contents = list(contents)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=self.contents.pop(0),
            refusal=None,
        ))])


class FakeClientFactory:
    def __init__(self, contents):
        self.completions = SequencedCompletions(contents)
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=self.completions))
        self.options = None

    def __call__(self, **kwargs):
        self.options = kwargs
        return self.client


class SequencedResponses:
    def __init__(self, contents):
        self.contents = list(contents)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(output_text=self.contents.pop(0), output=[])


class FakeResponsesClientFactory:
    def __init__(self, contents):
        self.responses = SequencedResponses(contents)
        self.client = SimpleNamespace(responses=self.responses)
        self.options = None

    def __call__(self, **kwargs):
        self.options = kwargs
        return self.client


def _settings():
    return SimpleNamespace(
        solution_provider="",
        solution_base_url="",
        solution_model="",
        solution_timeout_seconds=90.0,
        solution_reasoning_effort="low",
        solution_max_output_tokens=10_000,
        openai_api_key="",
        signal_analysis_provider="",
        signal_analysis_base_url="",
        signal_analysis_model="",
        signal_analysis_timeout_seconds=90.0,
        search_interpreter_provider="local",
        search_interpreter_base_url="http://127.0.0.1:8080/v1",
        search_interpreter_model="gpt-oss-20b",
        groq_api_key="",
    )


def _output():
    return {
        "mvpGoal": "Send one invoice reminder.",
        "constraints": ["Ship in 72 hours"],
        "nonGoals": ["Payment processing"],
        "primaryFlow": {
            "actor": "Freelancer",
            "trigger": "Invoice is overdue",
            "steps": ["Open invoice", "Send reminder"],
            "successOutcome": "Reminder is sent",
        },
        "features": [{
            "id": "feature-1",
            "title": "Send reminder",
            "summary": "Send a reminder for an overdue invoice.",
            "problemUnitIds": ["unit-1"],
            "evidenceIds": ["evidence-1"],
            "acceptanceCriteria": ["Reminder is recorded"],
        }],
        "architecture": {
            "stack": ["FastAPI"],
            "components": [{
                "id": "component-1",
                "name": "Reminder API",
                "responsibility": "Send reminders",
                "featureIds": ["feature-1"],
            }],
            "dataModel": [],
            "apiContracts": [],
            "integrations": [],
            "securityAndPrivacy": ["Enforce project ownership"],
        },
        "tasks": [{
            "id": "task-1",
            "title": "Implement reminder",
            "description": "Add the endpoint and persistence.",
            "featureIds": ["feature-1"],
            "dependsOn": [],
            "acceptanceChecks": ["Endpoint test passes"],
            "status": "todo",
        }],
        "testPlan": {"unit": [], "integration": [], "endToEnd": ["Send reminder"]},
        "deliveryChecklist": [{
            "id": "delivery-1",
            "label": "Deploy service",
            "kind": "deployment",
            "required": True,
            "status": "not_started",
        }],
    }


async def test_solution_provider_uses_strict_schema_and_repairs_once():
    factory = FakeClientFactory(["{}", json.dumps(_output())])
    provider = build_solution_provider(
        resolve_solution_provider_config(_settings()),
        client_factory=factory,
    )

    result = await provider.generate(instructions="Design", payload={"problemCase": {}})

    assert result.features[0].problem_unit_ids == ["unit-1"]
    assert len(factory.completions.requests) == 2
    request = factory.completions.requests[0]
    assert request["max_tokens"] == 10_000
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


def test_solution_provider_inherits_existing_local_provider_settings():
    config = resolve_solution_provider_config(_settings())

    assert config.name == "local"
    assert config.base_url == "http://127.0.0.1:8080/v1"
    assert config.model == "gpt-oss-20b"
    assert config.timeout_seconds == 90.0
    assert config.api_key == "local-not-a-secret"


async def test_openai_solution_provider_uses_responses_api_and_cost_controls():
    settings = _settings()
    settings.solution_provider = "openai"
    settings.solution_base_url = ""
    settings.solution_model = ""
    settings.openai_api_key = "test-key"
    settings.solution_reasoning_effort = "medium"
    settings.solution_max_output_tokens = 7_500
    factory = FakeResponsesClientFactory([json.dumps(_output())])

    config = resolve_solution_provider_config(settings)
    provider = build_solution_provider(config, client_factory=factory)
    result = await provider.generate(instructions="Design", payload={"problemCase": {}})

    assert result.mvp_goal == "Send one invoice reminder."
    assert config.model == OPENAI_DEFAULT_MODEL
    assert config.base_url == OPENAI_BASE_URL
    assert factory.options["api_key"] == "test-key"
    request = factory.responses.requests[0]
    assert request["model"] == OPENAI_DEFAULT_MODEL
    assert request["max_output_tokens"] == 7_500
    assert request["reasoning"] == {"effort": "medium"}
    assert request["store"] is False
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"]["additionalProperties"] is False


def test_openai_solution_provider_requires_credentials():
    settings = _settings()
    settings.solution_provider = "openai"
    settings.solution_base_url = ""
    settings.solution_model = ""

    with pytest.raises(SignalAnalysisProviderFailure) as error:
        resolve_solution_provider_config(settings)

    assert error.value.category == "missing_credentials"
