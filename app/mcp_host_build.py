"""Host-model Build preparation and persistence for MCP clients.

The MCP path deliberately avoids Immensity's configured solution provider. The
connected client model receives the grounded Problem Case plus the exact Build
schema, performs the reasoning, then sends the structured result back here for
validation, versioning, and persistence.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.build_documents import ensure_build_documents
from app.models import Pipeline, SolutionCase, SolutionTaskState, SolutionVersion
from app.prompts.solution_generation import SOLUTION_GENERATION_INSTRUCTIONS
from app.services.signal_cases import (
    apply_signal_overrides,
    build_current_case_document,
    find_current_version,
    find_signal_case,
    load_signal_overrides,
)
from app.services.solution_cases import find_solution_case, utc_now
from app.services.solution_worker import build_curated_problem_payload, validate_problem_traceability
from app.solution_contract import (
    SOLUTION_SCHEMA_VERSION,
    SolutionCaseDocument,
    SolutionGenerateRequest,
    SolutionModelOutput,
    SolutionProjectContext,
)


async def prepare_host_build_context(
    db: AsyncSession,
    card: Pipeline,
    uid: str,
    request: SolutionGenerateRequest,
) -> dict[str, Any]:
    """Return everything the connected model needs to design the Build."""

    signal_case = await find_signal_case(db, str(card.id), uid)
    signal_version = await find_current_version(db, signal_case) if signal_case else None
    if signal_case is None or signal_version is None:
        raise ValueError("Complete Analysis before designing the Build Contract")

    problem_document = build_current_case_document(card, signal_case, signal_version)
    overrides = await load_signal_overrides(db, signal_case.id, uid)
    problem_document = apply_signal_overrides(problem_document, overrides)
    curated_problem_case = build_curated_problem_payload(problem_document)
    if problem_document.version == 0 or not curated_problem_case["problemUnits"]:
        raise ValueError("Complete Analysis before designing the Build Contract")

    return {
        "projectId": str(card.id),
        "problemCaseVersion": problem_document.version,
        "instructions": SOLUTION_GENERATION_INSTRUCTIONS,
        "buildSchema": SolutionModelOutput.model_json_schema(by_alias=True),
        "context": {
            "request": request.model_dump(mode="json", by_alias=True),
            "problemCase": curated_problem_case,
        },
        "saveWith": "save_build_workspace",
    }


async def save_host_build_workspace(
    db: AsyncSession,
    card: Pipeline,
    uid: str,
    *,
    problem_case_version: int,
    request: SolutionGenerateRequest,
    build: dict[str, Any],
) -> dict[str, Any]:
    """Validate and persist a Build produced by the connected client model."""

    signal_case = await find_signal_case(db, str(card.id), uid)
    signal_version = await find_current_version(db, signal_case) if signal_case else None
    if signal_case is None or signal_version is None:
        raise ValueError("Complete Analysis before designing the Build Contract")

    problem_document = build_current_case_document(card, signal_case, signal_version)
    if problem_document.version != problem_case_version:
        raise ValueError(
            "The Problem Case changed during Build design. Generate the Build context again before saving."
        )

    overrides = await load_signal_overrides(db, signal_case.id, uid)
    problem_document = apply_signal_overrides(problem_document, overrides)
    curated_problem_case = build_curated_problem_payload(problem_document)
    if not curated_problem_case["problemUnits"]:
        raise ValueError("Complete Analysis before designing the Build Contract")

    try:
        output = SolutionModelOutput.model_validate(build)
        validate_problem_traceability(output, curated_problem_case)
    except ValidationError as exc:
        raise ValueError("Build does not match the required Immensity schema") from exc
    except ValueError as exc:
        raise ValueError(f"Build traceability validation failed: {exc}") from exc

    now = utc_now()
    case = await find_solution_case(db, str(card.id), uid)
    if case is None:
        case = SolutionCase(
            id=str(uuid.uuid4()),
            pipeline_id=str(card.id),
            user_id=uid,
            status="ready",
            progress_step=None,
            progress_label=None,
            safe_error=None,
            problem_case_version=problem_document.version,
            request=request.model_dump(mode="json", by_alias=True),
            current_version_id=None,
            approval_status="draft",
            approved_version_id=None,
            approved_at=None,
            created_at=now,
            updated_at=now,
        )
        db.add(case)
        await db.flush()
    else:
        case.request = request.model_dump(mode="json", by_alias=True)
        case.problem_case_version = problem_document.version
        case.safe_error = None

    next_version = int(
        await db.scalar(
            select(func.coalesce(func.max(SolutionVersion.version), 0)).where(
                SolutionVersion.case_id == case.id
            )
        )
        or 0
    ) + 1

    document = SolutionCaseDocument(
        version=next_version,
        status="ready",
        progress=None,
        safe_error=None,
        project=SolutionProjectContext(
            pipeline_id=str(card.id),
            project_name=card.project_name or card.name,
            delivery_targets=request.delivery_targets,
            generated_at=now.isoformat(),
        ),
        problem_case_version=problem_document.version,
        primary_users=[output.primary_flow.actor],
        mvp_capability=output.mvp_goal,
        mvp_outcome=output.primary_flow.success_outcome,
        **output.model_dump(),
    )
    document = ensure_build_documents(document)

    version = SolutionVersion(
        id=str(uuid.uuid4()),
        case_id=case.id,
        version=next_version,
        schema_version=SOLUTION_SCHEMA_VERSION,
        provider="host_agent",
        model="user_selected",
        problem_case_version=problem_document.version,
        document=document.model_dump(mode="json", by_alias=True),
        generated_at=now,
    )
    db.add(version)
    await db.flush()

    # A newly generated task graph must not inherit progress from an older Build.
    await db.execute(delete(SolutionTaskState).where(SolutionTaskState.case_id == case.id))
    case.current_version_id = version.id
    case.approval_status = "draft"
    case.approved_version_id = None
    case.approved_at = None
    case.status = "ready"
    case.progress_step = None
    case.progress_label = None
    case.safe_error = None
    case.problem_case_version = problem_document.version
    case.updated_at = now
    await db.commit()

    return {
        "projectId": str(card.id),
        "build": {
            "status": "ready",
            "version": next_version,
            "problemCaseVersion": problem_document.version,
            "approvalStatus": "draft",
            "featureCount": len(output.features),
            "taskCount": len(output.tasks),
        },
    }
