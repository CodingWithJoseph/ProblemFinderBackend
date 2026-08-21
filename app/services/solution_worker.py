"""Durable worker for versioned MVP Solution Case generation."""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.build_documents import ensure_build_documents
from app.db import AsyncSessionLocal
from app.models import Pipeline, SolutionCase, SolutionJob, SolutionTaskState, SolutionVersion
from app.prompts.solution_generation import SOLUTION_GENERATION_INSTRUCTIONS
from app.services.signal_analysis_provider import SignalAnalysisProviderFailure
from app.services.signal_cases import (
    apply_signal_overrides,
    build_current_case_document,
    find_current_version,
    find_signal_case,
    load_signal_overrides,
)
from app.services.solution_cases import utc_now
from app.services.solution_provider import SolutionProvider, get_solution_provider
from app.solution_contract import (
    SOLUTION_SCHEMA_VERSION,
    SolutionCaseDocument,
    SolutionGenerateRequest,
    SolutionModelOutput,
    SolutionProjectContext,
)


logger = logging.getLogger(__name__)
LEASE_SECONDS = 240
SAFE_FAILURES = {
    "timeout": "MVP solution generation timed out. Retry without losing the previous version.",
    "connection": "The solution model is unavailable. Retry later.",
    "missing_credentials": "Solution generation is not configured for this environment.",
    "invalid_configuration": "Solution generation is not configured for this environment.",
    "invalid_provider": "Solution generation is not configured for this environment.",
    "refusal": "The solution model could not process this Problem Case.",
    "empty_response": "Solution generation returned no usable result.",
    "invalid_json": "Solution generation returned an invalid result.",
    "schema_invalid": "Solution generation returned an invalid result.",
    "invalid_response": "Solution generation returned an invalid result.",
    "provider_error": "Solution generation could not be completed.",
    "validation": "Solution generation could not verify feature traceability.",
    "missing_case": "The project is no longer available.",
    "problem_case_not_ready": "Complete Analysis before generating a solution.",
}


async def lease_next_solution_job(
    db: AsyncSession,
    worker_id: str,
    *,
    now: datetime | None = None,
) -> str | None:
    leased_at = now or utc_now()
    job = (await db.execute(
        select(SolutionJob)
        .where(
            SolutionJob.attempt < SolutionJob.max_attempts,
            or_(
                SolutionJob.status == "queued",
                (
                    (SolutionJob.status == "running")
                    & (SolutionJob.lease_expires_at < leased_at)
                ),
            ),
        )
        .order_by(SolutionJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )).scalar_one_or_none()
    if job is None:
        return None

    job.status = "running"
    job.attempt = int(job.attempt or 0) + 1
    job.lease_owner = worker_id
    job.lease_expires_at = leased_at + timedelta(seconds=LEASE_SECONDS)
    job.started_at = job.started_at or leased_at
    job.updated_at = leased_at
    case = (await db.execute(
        select(SolutionCase).where(SolutionCase.id == job.case_id)
    )).scalar_one_or_none()
    if case is not None:
        case.status = "generating"
        case.progress_step = "preparing_context"
        case.progress_label = "Preparing the approved Problem Case"
        case.safe_error = None
        case.updated_at = leased_at
    await db.commit()
    return str(job.id)


async def process_solution_job(
    job_id: str,
    *,
    provider: SolutionProvider | None = None,
    settings: Settings | None = None,
) -> None:
    resolved_settings = settings or get_settings()
    try:
        resolved_provider = provider or get_solution_provider(resolved_settings)
    except SignalAnalysisProviderFailure as exc:
        async with AsyncSessionLocal() as db:
            await fail_solution_job(db, job_id, exc.category)
        return
    async with AsyncSessionLocal() as db:
        await execute_solution_job(db, job_id, resolved_provider)


async def execute_solution_job(
    db: AsyncSession,
    job_id: str,
    provider: SolutionProvider,
) -> None:
    job = (await db.execute(
        select(SolutionJob).where(SolutionJob.id == job_id)
    )).scalar_one_or_none()
    if job is None or job.status != "running":
        return
    case = (await db.execute(
        select(SolutionCase).where(SolutionCase.id == job.case_id)
    )).scalar_one_or_none()
    if case is None:
        await fail_solution_job(db, job_id, "missing_case", job=job)
        return
    card = (await db.execute(
        select(Pipeline).where(
            Pipeline.id == case.pipeline_id,
            Pipeline.user_id == case.user_id,
            Pipeline.removed_at == None,
        )
    )).scalar_one_or_none()
    if card is None:
        await fail_solution_job(db, job_id, "missing_case", job=job, case=case)
        return

    signal_case = await find_signal_case(db, str(card.id), case.user_id)
    signal_version = await find_current_version(db, signal_case) if signal_case else None
    if signal_case is None or signal_version is None:
        await fail_solution_job(db, job_id, "problem_case_not_ready", job=job, case=case)
        return
    problem_document = build_current_case_document(card, signal_case, signal_version)
    overrides = await load_signal_overrides(db, signal_case.id, case.user_id)
    problem_document = apply_signal_overrides(problem_document, overrides)
    if problem_document.version == 0 or not problem_document.problem_units:
        await fail_solution_job(db, job_id, "problem_case_not_ready", job=job, case=case)
        return

    request = SolutionGenerateRequest.model_validate(case.request or {})
    case.problem_case_version = problem_document.version
    case.progress_step = "designing_solution"
    case.progress_label = "Designing the 72-hour MVP"
    case.updated_at = utc_now()
    await db.commit()

    try:
        output = await provider.generate(
            instructions=SOLUTION_GENERATION_INSTRUCTIONS,
            payload={
                "request": request.model_dump(mode="json", by_alias=True),
                "problemCase": problem_document.model_dump(mode="json", by_alias=True),
            },
        )
        case.progress_step = "validating_traceability"
        case.progress_label = "Validating problem-to-feature traceability"
        await db.commit()
        validate_problem_traceability(output, problem_document)
        next_version = int(await db.scalar(
            select(func.coalesce(func.max(SolutionVersion.version), 0)).where(
                SolutionVersion.case_id == case.id
            )
        ) or 0) + 1
        document = SolutionCaseDocument(
            version=next_version,
            status="ready",
            progress=None,
            safe_error=None,
            project=SolutionProjectContext(
                pipeline_id=str(card.id),
                project_name=card.project_name or card.name,
                delivery_targets=request.delivery_targets,
                generated_at=utc_now().isoformat(),
            ),
            problem_case_version=problem_document.version,
            primary_users=[output.primary_flow.actor],
            mvp_capability=output.mvp_goal,
            mvp_outcome=output.primary_flow.success_outcome,
            **output.model_dump(),
        )
        await save_solution_version(db, job, case, document, provider)
    except SignalAnalysisProviderFailure as exc:
        await fail_solution_job(db, job_id, exc.category, job=job, case=case)
    except (ValidationError, ValueError):
        logger.warning("Solution traceability validation failed case=%s", case.id)
        await fail_solution_job(db, job_id, "validation", job=job, case=case)
    except Exception:
        logger.exception("Solution generation failed case=%s", case.id)
        await fail_solution_job(db, job_id, "provider_error", job=job, case=case)


def validate_problem_traceability(output: SolutionModelOutput, problem_document) -> None:
    known_units = {
        unit.id for unit in problem_document.problem_units if not unit.rejected
    }
    known_evidence = {record.id for record in problem_document.evidence}
    for feature in output.features:
        unknown_units = sorted(set(feature.problem_unit_ids) - known_units)
        unknown_evidence = sorted(set(feature.evidence_ids) - known_evidence)
        if unknown_units:
            raise ValueError(
                f"feature {feature.id} contains unknown problem units: {', '.join(unknown_units)}"
            )
        if unknown_evidence:
            raise ValueError(
                f"feature {feature.id} contains unknown evidence: {', '.join(unknown_evidence)}"
            )


async def save_solution_version(
    db: AsyncSession,
    job: SolutionJob,
    case: SolutionCase,
    document: SolutionCaseDocument,
    provider: SolutionProvider,
) -> None:
    now = utc_now()
    document = ensure_build_documents(document)
    version = SolutionVersion(
        id=str(uuid.uuid4()),
        case_id=case.id,
        version=document.version,
        schema_version=SOLUTION_SCHEMA_VERSION,
        provider=provider.name,
        model=provider.model,
        problem_case_version=document.problem_case_version,
        document=document.model_dump(mode="json", by_alias=True),
        generated_at=now,
    )
    db.add(version)
    await db.flush()
    # Progress belongs to the generated task graph. A successful regeneration
    # replaces that graph, so old task IDs must not inherit completed states.
    await db.execute(delete(SolutionTaskState).where(SolutionTaskState.case_id == case.id))
    case.current_version_id = version.id
    case.approval_status = "draft"
    case.approved_version_id = None
    case.approved_at = None
    case.status = "ready"
    case.progress_step = None
    case.progress_label = None
    case.safe_error = None
    case.problem_case_version = document.problem_case_version
    case.updated_at = now
    job.status = "succeeded"
    job.finished_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = now
    await db.commit()


async def fail_solution_job(
    db: AsyncSession,
    job_id: str,
    category: str,
    *,
    job: SolutionJob | None = None,
    case: SolutionCase | None = None,
) -> None:
    failed_job = job or (await db.execute(
        select(SolutionJob).where(SolutionJob.id == job_id)
    )).scalar_one_or_none()
    if failed_job is None:
        return
    failed_case = case or (await db.execute(
        select(SolutionCase).where(SolutionCase.id == failed_job.case_id)
    )).scalar_one_or_none()
    now = utc_now()
    safe_error = SAFE_FAILURES.get(category, SAFE_FAILURES["provider_error"])
    failed_job.status = "failed"
    failed_job.error_category = category
    failed_job.safe_error = safe_error
    failed_job.finished_at = now
    failed_job.lease_owner = None
    failed_job.lease_expires_at = None
    failed_job.updated_at = now
    if failed_case is not None:
        failed_case.status = "failed"
        failed_case.progress_step = None
        failed_case.progress_label = None
        failed_case.safe_error = safe_error
        failed_case.updated_at = now
    await db.commit()


async def run_solution_worker(
    stop_event: asyncio.Event,
    *,
    settings: Settings | None = None,
    worker_id: str | None = None,
) -> None:
    resolved = settings or get_settings()
    identity = worker_id or f"{socket.gethostname()}:{uuid.uuid4()}"
    poll_seconds = max(0.25, min(float(resolved.signal_analysis_poll_seconds), 30.0))
    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as db:
                job_id = await lease_next_solution_job(db, identity)
            if job_id:
                await process_solution_job(job_id, settings=resolved)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Solution worker iteration failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass
