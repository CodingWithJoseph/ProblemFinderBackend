import copy
import io
import zipfile

import pytest
from pydantic import ValidationError

from app.solution_contract import SOLUTION_SCHEMA_VERSION, SolutionCaseDocument
from app.build_documents import build_export_zip, ensure_build_documents, replace_build_document


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


def test_ready_solution_renders_only_the_focused_build_package():
    document = ensure_build_documents(SolutionCaseDocument.model_validate(_document()))

    assert [item.filename for item in document.documents] == [
        "docs/BUILD_CONTRACT.md",
        "docs/FEATURES.md",
        "docs/REQUIREMENTS.md",
        "docs/TASKS.md",
        "docs/TEST_PLAN.md",
        "docs/DECISIONS.md",
        "AGENTS.md",
        "CLAUDE.md",
        ".github/copilot-instructions.md",
    ]
    assert "willingness to pay" not in document.documents[0].content.lower()
    assert next(item for item in document.documents if item.key == "agents").editable is False


def test_document_edit_creates_a_draft_copy_without_mutating_the_source():
    source = ensure_build_documents(SolutionCaseDocument.model_validate(_document()))
    requirement_document = next(
        item for item in source.documents if item.key == "requirements"
    )
    content = requirement_document.content.replace(
        "A reminder can be sent from an overdue invoice",
        "A reminder can be sent with an audit timestamp",
    )
    edited = replace_build_document(
        source,
        "requirements",
        content,
        next_version=2,
    )

    assert source.version == 1
    assert edited.version == 2
    assert edited.approval_status == "draft"
    assert "audit timestamp" in edited.features[0].acceptance_criteria[0]
    assert "audit timestamp" in next(
        item for item in edited.documents if item.key == "features"
    ).content
    with pytest.raises(PermissionError):
        replace_build_document(source, "agents", "changed", next_version=2)


def test_build_contract_edit_updates_the_structured_contract():
    source = ensure_build_documents(SolutionCaseDocument.model_validate(_document()))
    build_contract = next(
        item for item in source.documents if item.key == "build_contract"
    )
    content = build_contract.content.replace(
        "Let a freelancer send and track an invoice reminder.",
        "Let a freelancer send and verify an invoice reminder.",
    ).replace(
        "1. Open invoice",
        "1. Open and verify invoice",
    )

    edited = replace_build_document(source, "build_contract", content, next_version=2)

    assert edited.mvp_goal == "Let a freelancer send and verify an invoice reminder."
    assert edited.primary_flow.steps[0] == "Open and verify invoice"


def test_build_contract_edit_cannot_rewrite_problem_case_provenance():
    source = ensure_build_documents(SolutionCaseDocument.model_validate(_document()))
    build_contract = next(
        item for item in source.documents if item.key == "build_contract"
    )

    with pytest.raises(ValueError, match="Problem Case version cannot be changed"):
        replace_build_document(
            source,
            "build_contract",
            build_contract.content.replace("Problem Case version: 2", "Problem Case version: 99"),
            next_version=2,
        )


def test_feature_edit_regenerates_derived_requirements():
    source = ensure_build_documents(SolutionCaseDocument.model_validate(_document()))
    feature_document = next(item for item in source.documents if item.key == "features")
    content = feature_document.content.replace(
        "A reminder can be sent from an overdue invoice",
        "A reminder can be sent idempotently from an overdue invoice",
    )

    edited = replace_build_document(source, "features", content, next_version=2)

    assert "idempotently" in next(
        item for item in edited.documents if item.key == "requirements"
    ).content


def test_freeform_requirement_edit_is_rejected_instead_of_drifting():
    source = ensure_build_documents(SolutionCaseDocument.model_validate(_document()))

    with pytest.raises(ValueError, match="requirement table rows"):
        replace_build_document(
            source,
            "requirements",
            "# Requirements\n\nFreeform text\n",
            next_version=2,
        )


def test_task_markdown_edits_update_the_structured_task_graph():
    source = ensure_build_documents(SolutionCaseDocument.model_validate(_document()))
    task_document = next(item for item in source.documents if item.key == "tasks")
    edited_content = task_document.content.replace(
        "task-1: Create reminder endpoint",
        "task-1: Create verified reminder endpoint",
    ).replace(
        "Implement the authenticated reminder API.",
        "Implement and verify the authenticated reminder API.",
    )

    edited = replace_build_document(source, "tasks", edited_content, next_version=2)

    assert edited.tasks[0].title == "Create verified reminder endpoint"
    assert edited.tasks[0].description == "Implement and verify the authenticated reminder API."
    assert edited.tasks[0].acceptance_checks == ["Endpoint test passes"]


def test_structurally_invalid_task_edit_is_rejected_instead_of_drifting():
    source = ensure_build_documents(SolutionCaseDocument.model_validate(_document()))

    with pytest.raises(ValueError, match="TASKS.md requires"):
        replace_build_document(source, "tasks", "# Tasks\n\nFreeform text", next_version=2)


def test_build_export_contains_markdown_agent_files_and_canonical_json():
    document = ensure_build_documents(SolutionCaseDocument.model_validate(_document()))
    filename, content = build_export_zip(document)

    assert filename == "invoice-helper-build-v1.zip"
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert "docs/BUILD_CONTRACT.md" in archive.namelist()
        assert "AGENTS.md" in archive.namelist()
        assert ".github/copilot-instructions.md" in archive.namelist()
        assert '"problemCaseVersion": 2' in archive.read("build.json").decode("utf-8")
