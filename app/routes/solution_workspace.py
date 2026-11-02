"""Build workspace endpoints backed by immutable Solution Case versions."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.auth import get_uid
from app.build_documents import build_export_zip
from app.db import get_db
from app.models import SolutionTaskState
from app.routes.pipeline import _get_pipeline_card
from app.services.signal_cases import find_current_version, find_signal_case
from app.services.solution_cases import (
    build_current_solution_document,
    build_empty_solution_document,
    find_current_solution_version,
    find_solution_case,
    find_solution_case_for_update,
    new_solution_case_and_job,
    queue_solution_generation,
)
from app.services.solution_versions import (
    approve_solution_version,
    save_solution_document_version,
    save_solution_section_version,
    solution_with_task_states,
)
from app.solution_contract import (
    ApproveSolutionRequest,
    BuildDocumentUpdateRequest,
    SolutionCaseDocument,
    SolutionGenerateRequest,
    SolutionSectionKey,
    SolutionSectionUpdateRequest,
    SolutionTaskUpdateRequest,
)


router = APIRouter(prefix="/pipeline", tags=["solution"])


@router.get(
    "/{pipeline_id}/solution/case",
    response_model=SolutionCaseDocument,
)
async def get_solution_case(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_solution_case(db, pipeline_id, uid)
    if case is None:
        return build_empty_solution_document(card)
    request = SolutionGenerateRequest.model_validate(case.request or {})
    current = await find_current_solution_version(db, case)
    if current is None:
        return build_empty_solution_document(
            card,
            status=case.status,
            request=request,
            case=case,
        )
    return await solution_with_task_states(db, card, case, current)


@router.post(
    "/{pipeline_id}/solution/case/generate",
    response_model=SolutionCaseDocument,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_solution_case(
    pipeline_id: str,
    body: SolutionGenerateRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_pipeline_card(pipeline_id, db, uid)
    problem_case = await find_signal_case(db, pipeline_id, uid)
    problem_version = await find_current_version(db, problem_case) if problem_case else None
    if problem_case is None or problem_version is None:
        raise HTTPException(
            status_code=409,
            detail="Complete Analysis before generating a solution",
        )

    case = await find_solution_case(db, pipeline_id, uid)
    if case is None:
        case, job = new_solution_case_and_job(
            card,
            uid,
            body,
            problem_version.version,
        )
        db.add(case)
        db.add(job)
        await db.commit()
    else:
        await queue_solution_generation(
            db,
            case,
            uid,
            body,
            problem_version.version,
        )

    current = await find_current_solution_version(db, case)
    if current is not None:
        return build_current_solution_document(card, case, current)
    return build_empty_solution_document(
        card,
        status=case.status,
        request=body,
        case=case,
    )


@router.patch(
    "/{pipeline_id}/solution/case/sections/{section_key}",
    response_model=SolutionCaseDocument,
)
async def update_solution_section(
    pipeline_id: str,
    section_key: SolutionSectionKey,
    body: SolutionSectionUpdateRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_solution_case_for_update(db, pipeline_id, uid)
    current = await find_current_solution_version(db, case) if case else None
    if case is None or current is None or case.status != "ready":
        raise HTTPException(status_code=409, detail="Generate the Build Contract before editing it")
    return await save_solution_section_version(
        db,
        card,
        case,
        current,
        section_key,
        body.value,
        expected_version=body.expected_version,
        provider="user",
        model="structured-editor",
    )


@router.patch(
    "/{pipeline_id}/solution/case/documents/{document_key}",
    response_model=SolutionCaseDocument,
)
async def update_solution_document(
    pipeline_id: str,
    document_key: str,
    body: BuildDocumentUpdateRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_solution_case_for_update(db, pipeline_id, uid)
    current = await find_current_solution_version(db, case) if case else None
    if case is None or current is None or case.status != "ready":
        raise HTTPException(status_code=409, detail="Generate the Build Contract before editing it")
    return await save_solution_document_version(
        db,
        card,
        case,
        current,
        document_key,
        body.content,
        expected_version=body.expected_version,
        provider="user",
        model="manual",
    )


@router.post(
    "/{pipeline_id}/solution/case/approve",
    response_model=SolutionCaseDocument,
)
async def approve_solution_case(
    pipeline_id: str,
    body: ApproveSolutionRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_solution_case_for_update(db, pipeline_id, uid)
    current = await find_current_solution_version(db, case) if case else None
    if case is None or current is None or case.status != "ready":
        raise HTTPException(status_code=409, detail="Generate the Build Contract before approving it")
    return await approve_solution_version(
        db,
        card,
        case,
        current,
        expected_version=body.expected_version,
    )


@router.get("/{pipeline_id}/solution/case/export")
async def export_solution_case(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_solution_case(db, pipeline_id, uid)
    current = await find_current_solution_version(db, case) if case else None
    if case is None or current is None or case.status != "ready":
        raise HTTPException(status_code=409, detail="Generate the Build Contract before exporting it")
    filename, content = build_export_zip(build_current_solution_document(card, case, current))
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/{pipeline_id}/solution/case/tasks/{task_id}")
async def update_solution_task_progress(
    pipeline_id: str,
    task_id: str,
    body: SolutionTaskUpdateRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Save app-authenticated task progress without changing the approved plan/version."""
    card = await _get_pipeline_card(pipeline_id, db, uid)
    # Serialize with document edits so a task cannot be written against a replaced plan.
    case = await find_solution_case_for_update(db, pipeline_id, uid)
    current = await find_current_solution_version(db, case) if case else None
    if case is None or current is None or case.status != "ready":
        raise HTTPException(status_code=409, detail="Generate the Build Contract before updating tasks")
    if current.version != body.expected_version:
        raise HTTPException(status_code=409, detail="The Build Contract changed. Reload before updating tasks.")
    if case.approval_status != "approved" or case.approved_version_id != current.id:
        raise HTTPException(status_code=409, detail="Approve the current Build Contract before updating tasks")
    document = build_current_solution_document(card, case, current)
    if task_id not in {task.id for task in document.tasks}:
        raise HTTPException(status_code=404, detail="Solution task not found")
    row = (await db.execute(select(SolutionTaskState).where(
        SolutionTaskState.case_id == case.id,
        SolutionTaskState.task_id == task_id,
    ))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        row = SolutionTaskState(id=str(uuid.uuid4()), case_id=case.id, task_id=task_id, created_at=now)
        db.add(row)
    row.status = body.status
    row.note = body.note
    row.verification = body.verification.model_dump(mode="json", by_alias=True) if body.verification else {}
    row.updated_by = "app"
    row.updated_at = now
    await db.commit()
    return {"taskId": task_id, "status": row.status, "updatedAt": now.isoformat()}
