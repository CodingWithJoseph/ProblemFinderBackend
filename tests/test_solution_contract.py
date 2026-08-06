import copy

import pytest
from pydantic import ValidationError

from app.solution_contract import SOLUTION_SCHEMA_VERSION, SolutionCaseDocument


def _document() -> dict:
    return {
        "version": 1,
        "status": "ready",
        "progress": None,
        "safeError": None,
        "project": {
            "pipelineId": "pipeline-1",
            "projectName": "Invoice helper",
            "deliveryTarget": "web",
            "generatedAt": "2026-08-05T10:00:00+00:00",
        },
        "problemCaseVersion": 2,
        "mvpGoal": "Let a freelancer send and track an invoice reminder.",
        "constraints": ["Ship within 72 hours"],
        "nonGoals": ["Payment processing", "Market-fit scoring"],
        "primaryFlow": {
            "actor": "Freelancer",
            "trigger": "An invoice becomes overdue",
            "steps": ["Open invoice", "Send reminder", "See reminder status"],
            "successOutcome": "The reminder is recorded and delivered.",
        },
        "features": [{
            "id": "feature-1",
            "title": "Send invoice reminder",
            "summary": "Send a templated reminder and retain its status.",
            "problemUnitIds": ["unit-1"],
            "evidenceIds": ["evidence-1"],
            "acceptanceCriteria": ["A reminder can be sent from an overdue invoice"],
        }],
        "architecture": {
            "stack": ["Next.js", "FastAPI", "PostgreSQL"],
            "components": [{
                "id": "component-1",
                "name": "Reminder API",
                "responsibility": "Create reminders and track their state.",
                "featureIds": ["feature-1"],
            }],
            "dataModel": [{
                "name": "Reminder",
                "purpose": "Tracks one reminder attempt.",
                "fields": ["invoice_id", "status", "sent_at"],
            }],
            "apiContracts": [{
                "method": "POST",
                "path": "/reminders",
                "purpose": "Send a reminder.",
                "featureIds": ["feature-1"],
            }],
            "integrations": ["Email provider"],
            "securityAndPrivacy": ["Users can only access their own invoices"],
        },
        "tasks": [{
            "id": "task-1",
            "title": "Create reminder endpoint",
            "description": "Implement the authenticated reminder API.",
            "featureIds": ["feature-1"],
            "dependsOn": [],
            "acceptanceChecks": ["Endpoint test passes"],
            "status": "todo",
        }],
        "testPlan": {
            "unit": ["Reminder validation"],
            "integration": ["Reminder persistence"],
            "endToEnd": ["Send a reminder from an overdue invoice"],
        },
        "deliveryChecklist": [{
            "id": "delivery-1",
            "label": "Production health check passes",
            "kind": "deployment",
            "required": True,
            "status": "not_started",
        }],
    }


def test_solution_contract_accepts_traceable_five_feature_maximum():
    document = SolutionCaseDocument.model_validate(_document())
    payload = document.model_dump(mode="json", by_alias=True)

    assert SOLUTION_SCHEMA_VERSION == "solution-case.v1"
    assert payload["problemCaseVersion"] == 2
    assert payload["features"][0]["problemUnitIds"] == ["unit-1"]
    assert payload["tasks"][0]["status"] == "todo"


def test_solution_contract_rejects_untraceable_feature():
    payload = _document()
    payload["features"][0]["evidenceIds"] = []

    with pytest.raises(ValidationError):
        SolutionCaseDocument.model_validate(payload)


def test_solution_contract_rejects_unknown_task_feature():
    payload = _document()
    payload["tasks"][0]["featureIds"] = ["missing"]

    with pytest.raises(ValidationError, match="unknown ids: missing"):
        SolutionCaseDocument.model_validate(payload)


def test_solution_contract_rejects_more_than_five_mvp_features():
    payload = _document()
    feature = payload["features"][0]
    payload["features"] = [
        {**copy.deepcopy(feature), "id": f"feature-{index}"}
        for index in range(1, 7)
    ]
    payload["architecture"]["components"][0]["featureIds"] = []
    payload["architecture"]["apiContracts"][0]["featureIds"] = []
    payload["tasks"][0]["featureIds"] = ["feature-1"]

    with pytest.raises(ValidationError):
        SolutionCaseDocument.model_validate(payload)


def test_solution_contract_rejects_cyclic_task_dependencies():
    payload = _document()
    first_task = payload["tasks"][0]
    payload["tasks"] = [
        {**copy.deepcopy(first_task), "id": "task-1", "dependsOn": ["task-2"]},
        {**copy.deepcopy(first_task), "id": "task-2", "dependsOn": ["task-1"]},
    ]

    with pytest.raises(ValidationError, match="task dependencies must be acyclic"):
        SolutionCaseDocument.model_validate(payload)
