import json
from types import SimpleNamespace

from app.services.signal_analysis_provider import resolve_signal_analysis_provider_config
from app.services.solution_provider import build_solution_provider


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

    def __call__(self, **kwargs):
        return self.client


def _settings():
    return SimpleNamespace(
        signal_analysis_provider="local",
        signal_analysis_base_url="http://127.0.0.1:8080/v1",
        signal_analysis_model="gpt-oss-20b",
        signal_analysis_timeout_seconds=90.0,
        search_interpreter_provider="local",
        search_interpreter_base_url="",
        search_interpreter_model="",
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
        resolve_signal_analysis_provider_config(_settings()),
        client_factory=factory,
    )

    result = await provider.generate(instructions="Design", payload={"problemCase": {}})

    assert result.features[0].problem_unit_ids == ["unit-1"]
    assert len(factory.completions.requests) == 2
    request = factory.completions.requests[0]
    assert request["max_tokens"] == 10_000
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
