"""Create one local project with a ready Build package for UI testing."""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import datetime, timezone

from app.build_documents import ensure_build_documents
from app.config import get_settings
from app.models import Pipeline, SolutionCase as SolutionCaseRow, SolutionVersion
from app.solution_contract import SOLUTION_SCHEMA_VERSION, SolutionCaseDocument


SEED_VERSION = "build-ui-v1"


def test_project_ids(user_id: str) -> tuple[str, str, str]:
    """Return stable IDs so rerunning the seed does not create duplicates."""
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, "https://useimmensity.com/local-test-data")
    pipeline_id = str(uuid.uuid5(namespace, f"{user_id}:{SEED_VERSION}:pipeline"))
    case_id = str(uuid.uuid5(namespace, f"{user_id}:{SEED_VERSION}:case"))
    version_id = str(uuid.uuid5(namespace, f"{user_id}:{SEED_VERSION}:version:1"))
    return pipeline_id, case_id, version_id


def build_test_document(user_id: str, generated_at: datetime | None = None) -> SolutionCaseDocument:
    """Build representative, schema-valid data for every current Build screen."""
    pipeline_id, _, _ = test_project_ids(user_id)
    now = generated_at or datetime.now(timezone.utc)
    document = SolutionCaseDocument.model_validate({
        "version": 1,
        "status": "ready",
        "progress": None,
        "safeError": None,
        "project": {
            "pipelineId": pipeline_id,
            "projectName": "Invoice Reminder",
            "deliveryTarget": "web",
            "generatedAt": now.isoformat(),
        },
        "problemCaseVersion": 1,
        "approvalStatus": "draft",
        "approvedVersion": None,
        "approvedAt": None,
        "mvpGoal": "Help a freelancer send and track an overdue invoice reminder.",
        "constraints": [
            "Web-only first release",
            "Use the existing account authentication",
            "Keep one primary reminder flow",
        ],
        "nonGoals": [
            "Payment processing",
            "Accounting platform synchronization",
            "Automated collections",
        ],
        "primaryFlow": {
            "actor": "Freelancer",
            "trigger": "An invoice becomes overdue",
            "steps": [
                "Open the overdue invoice",
                "Review the reminder message",
                "Send the reminder",
                "See the delivery status",
            ],
            "successOutcome": "The reminder is recorded and its delivery status is visible.",
        },
        "features": [
            {
                "id": "feature-overdue-list",
                "title": "View overdue invoices",
                "summary": "Show the signed-in freelancer which invoices require follow-up.",
                "problemUnitIds": ["problem-unit-overdue-tracking"],
                "evidenceIds": ["evidence-manual-spreadsheet"],
                "acceptanceCriteria": [
                    "Only the signed-in freelancer's overdue invoices are shown",
                    "Each invoice shows customer, amount, and due date",
                ],
            },
            {
                "id": "feature-send-reminder",
                "title": "Send an invoice reminder",
                "summary": "Send a reviewed reminder and retain its delivery state.",
                "problemUnitIds": ["problem-unit-manual-follow-up"],
                "evidenceIds": ["evidence-copy-paste-reminders"],
                "acceptanceCriteria": [
                    "A reminder can be sent from an overdue invoice",
                    "The delivery attempt and status are recorded",
                ],
            },
        ],
        "architecture": {
            "stack": ["Next.js", "FastAPI", "PostgreSQL"],
            "components": [
                {
                    "id": "component-invoice-workspace",
                    "name": "Invoice workspace",
                    "responsibility": "Present overdue invoices and the reminder flow.",
                    "featureIds": ["feature-overdue-list", "feature-send-reminder"],
                },
                {
                    "id": "component-reminder-api",
                    "name": "Reminder API",
                    "responsibility": "Authorize, send, and record invoice reminders.",
                    "featureIds": ["feature-send-reminder"],
                },
            ],
            "dataModel": [
                {
                    "name": "Invoice",
                    "purpose": "Store the invoice owner, amount, due date, and payment state.",
                    "fields": ["id", "user_id", "customer_name", "amount", "due_date", "status"],
                },
                {
                    "name": "Reminder",
                    "purpose": "Record one reminder delivery attempt.",
                    "fields": ["id", "invoice_id", "message", "status", "sent_at"],
                },
            ],
            "apiContracts": [
                {
                    "method": "GET",
                    "path": "/invoices?status=overdue",
                    "purpose": "List the signed-in freelancer's overdue invoices.",
                    "featureIds": ["feature-overdue-list"],
                },
                {
                    "method": "POST",
                    "path": "/invoices/{invoice_id}/reminders",
                    "purpose": "Send and record an invoice reminder.",
                    "featureIds": ["feature-send-reminder"],
                },
            ],
            "integrations": ["Transactional email provider"],
            "securityAndPrivacy": [
                "Every invoice and reminder query is scoped to the authenticated user",
                "Reminder content is not written to application logs",
            ],
        },
        "tasks": [
            {
                "id": "task-overdue-query",
                "title": "Add the overdue invoice query",
                "description": "Return only overdue invoices owned by the authenticated user.",
                "featureIds": ["feature-overdue-list"],
                "dependsOn": [],
                "acceptanceChecks": ["Owned overdue invoices are returned in due-date order"],
                "status": "todo",
            },
            {
                "id": "task-reminder-endpoint",
                "title": "Add the reminder endpoint",
                "description": "Send the reviewed message and persist its delivery attempt.",
                "featureIds": ["feature-send-reminder"],
                "dependsOn": ["task-overdue-query"],
                "acceptanceChecks": ["An authorized request records one delivery attempt"],
                "status": "todo",
            },
            {
                "id": "task-reminder-screen",
                "title": "Build the reminder flow",
                "description": "Let the user select an overdue invoice, review the message, and send it.",
                "featureIds": ["feature-overdue-list", "feature-send-reminder"],
                "dependsOn": ["task-overdue-query", "task-reminder-endpoint"],
                "acceptanceChecks": ["The complete primary flow passes in the browser"],
                "status": "todo",
            },
        ],
        "testPlan": {
            "unit": [
                "Overdue status and due-date filtering",
                "Reminder message validation",
            ],
            "integration": [
                "Authenticated invoice ownership filtering",
                "Reminder delivery attempt persistence",
            ],
            "endToEnd": [
                "Send a reminder from an overdue invoice and see its delivery status",
            ],
        },
        "deliveryChecklist": [
            {
                "id": "delivery-primary-flow",
                "label": "Primary reminder flow passes end to end",
                "kind": "test",
                "required": True,
                "status": "not_started",
            },
        ],
    })
    return ensure_build_documents(document)


def require_local_environment(environment: str) -> None:
    if environment.strip().lower() in {"prod", "production"}:
        raise RuntimeError("Build test data cannot be seeded in production")


async def seed_build_test_project(user_id: str) -> str:
    settings = get_settings()
    require_local_environment(settings.environment)

    from app.db import AsyncSessionLocal

    pipeline_id, case_id, version_id = test_project_ids(user_id)
    now = datetime.now(timezone.utc)
    document = build_test_document(user_id, now)

    async with AsyncSessionLocal() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if pipeline is None:
            pipeline = Pipeline(id=pipeline_id)
            db.add(pipeline)
        elif pipeline.user_id != user_id:
            raise RuntimeError("The deterministic test project ID belongs to another user")
        pipeline.user_id = user_id
        pipeline.name = "Invoice reminder follow-up"
        pipeline.project_name = "Invoice Reminder"
        pipeline.post_ids = ["test-evidence-manual-spreadsheet", "test-evidence-copy-paste-reminders"]
        pipeline.source_cluster_id = None
        pipeline.stage = "building"
        pipeline.launched_at = now
        pipeline.removed_at = None
        pipeline.updated_at = now
        await db.flush()

        case = await db.get(SolutionCaseRow, case_id)
        if case is None:
            case = SolutionCaseRow(id=case_id, pipeline_id=pipeline_id, user_id=user_id)
            db.add(case)
        case.status = "ready"
        case.progress_step = None
        case.progress_label = None
        case.safe_error = None
        case.problem_case_version = 1
        case.request = {
            "deliveryTarget": "web",
            "constraints": document.constraints,
            "preferredStack": document.architecture.stack if document.architecture else [],
        }
        case.approval_status = "draft"
        case.approved_version_id = None
        case.approved_at = None
        case.updated_at = now
        await db.flush()

        version = await db.get(SolutionVersion, version_id)
        if version is None:
            version = SolutionVersion(id=version_id, case_id=case_id)
            db.add(version)
        version.version = 1
        version.schema_version = SOLUTION_SCHEMA_VERSION
        version.provider = "local_seed"
        version.model = SEED_VERSION
        version.problem_case_version = 1
        version.document = document.model_dump(mode="json", by_alias=True)
        version.generated_at = now
        await db.flush()

        case.current_version_id = version_id
        await db.commit()

    return pipeline_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a local project with representative data for every Build screen.",
    )
    parser.add_argument("--user-id", required=True, help="Firebase UID that should own the test project")
    args = parser.parse_args()
    pipeline_id = asyncio.run(seed_build_test_project(args.user_id.strip()))
    print(f"Seeded local Build test project: {pipeline_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
