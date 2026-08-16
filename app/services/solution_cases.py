"""Persistence helpers for versioned MVP solution cases."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, SolutionCase, SolutionJob, SolutionVersion
from app.build_documents import ensure_build_documents
from app.solution_contract import (
    SolutionCaseDocument,
    SolutionGenerateRequest,
    SolutionProgress,
    SolutionProjectContext,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_solution_case_and_job(
    card: Pipeline,
    uid: str,
    request: SolutionGenerateRequest,
    problem_case_version: int,
) -> tuple[SolutionCase, SolutionJob]:
    now = utc_now()
    case = SolutionCase(
        id=str(uuid.uuid4()),
        pipeline_id=str(card.id),
        user_id=uid,
        status="queued",
        progress_step="queued",
        progress_label="Waiting to design the MVP",
        safe_error=None,
        problem_case_version=problem_case_version,
        request=request.model_dump(mode="json", by_alias=True),
        current_version_id=None,
        approval_status="draft",
        approved_version_id=None,
        approved_at=None,
        created_at=now,
        updated_at=now,
    )
    job = SolutionJob(
        id=str(uuid.uuid4()),
        case_id=case.id,
        requested_by=uid,
        status="queued",
        attempt=0,
        max_attempts=2,
        created_at=now,
        updated_at=now,
    )
    return case, job


def build_empty_solution_document(
    card: Pipeline,
    *,
    status: str = "not_started",
    request: SolutionGenerateRequest | None = None,
    case: SolutionCase | None = None,
) -> SolutionCaseDocument:
    target = request.delivery_target if request else "web"
    progress = None
    if case and case.progress_step and case.progress_label:
        progress = SolutionProgress(step=case.progress_step, label=case.progress_label)
    return SolutionCaseDocument(
        version=0,
        status=status,
        progress=progress,
        safe_error=case.safe_error if case else None,
        project=SolutionProjectContext(
            pipeline_id=str(card.id),
            project_name=card.project_name or card.name,
            delivery_target=target,
            generated_at=None,
        ),
        problem_case_version=case.problem_case_version if case else 0,
        approval_status=getattr(case, "approval_status", None) or "draft",
        approved_at=(
            case.approved_at.isoformat()
            if case and getattr(case, "approved_at", None)
            else None
        ),
    )


def build_current_solution_document(
    card: Pipeline,
    case: SolutionCase,
    version: SolutionVersion,
) -> SolutionCaseDocument:
    payload = dict(version.document or {})
    payload.update({
        "version": version.version,
        "status": case.status,
        "progress": (
            {"step": case.progress_step, "label": case.progress_label}
            if case.progress_step and case.progress_label
            else None
        ),
        "safeError": case.safe_error,
        "problemCaseVersion": version.problem_case_version,
        "approvalStatus": getattr(case, "approval_status", None) or "draft",
        "approvedVersion": (
            version.version
            if getattr(case, "approved_version_id", None) == version.id
            else None
        ),
        "approvedAt": (
            case.approved_at.isoformat()
            if getattr(case, "approved_at", None)
            else None
        ),
        "project": {
            "pipelineId": str(card.id),
            "projectName": card.project_name or card.name,
            "deliveryTarget": payload.get("project", {}).get("deliveryTarget", "web"),
            "generatedAt": version.generated_at.isoformat() if version.generated_at else None,
        },
    })
    return ensure_build_documents(SolutionCaseDocument.model_validate(payload))


async def find_solution_case(
    db: AsyncSession,
    pipeline_id: str,
    uid: str,
) -> SolutionCase | None:
    return (await db.execute(
        select(SolutionCase).where(
            SolutionCase.pipeline_id == pipeline_id,
            SolutionCase.user_id == uid,
        )
    )).scalar_one_or_none()


async def find_solution_case_for_update(
    db: AsyncSession,
    pipeline_id: str,
    uid: str,
) -> SolutionCase | None:
    return (await db.execute(
        select(SolutionCase)
        .where(
            SolutionCase.pipeline_id == pipeline_id,
            SolutionCase.user_id == uid,
        )
        .with_for_update()
    )).scalar_one_or_none()


async def find_current_solution_version(
    db: AsyncSession,
    case: SolutionCase,
) -> SolutionVersion | None:
    if not case.current_version_id:
        return None
    return (await db.execute(
        select(SolutionVersion).where(SolutionVersion.id == case.current_version_id)
    )).scalar_one_or_none()


async def queue_solution_generation(
    db: AsyncSession,
    case: SolutionCase,
    uid: str,
    request: SolutionGenerateRequest,
    problem_case_version: int,
) -> SolutionJob:
    active = (await db.execute(
        select(SolutionJob).where(
            SolutionJob.case_id == case.id,
            SolutionJob.status.in_(("queued", "running")),
        )
    )).scalar_one_or_none()
    if active is not None:
        return active

    now = utc_now()
    case.status = "queued"
    case.progress_step = "queued"
    case.progress_label = "Waiting to design the MVP"
    case.safe_error = None
    case.problem_case_version = problem_case_version
    case.request = request.model_dump(mode="json", by_alias=True)
    case.updated_at = now
    job = SolutionJob(
        id=str(uuid.uuid4()),
        case_id=case.id,
        requested_by=uid,
        status="queued",
        attempt=0,
        max_attempts=2,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    await db.commit()
    return job
