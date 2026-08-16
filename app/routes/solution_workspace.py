"""Build workspace endpoints backed by immutable Solution Case versions."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.build_documents import build_export_zip, replace_build_document
from app.db import get_db
from app.models import SolutionVersion
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
    utc_now,
)
from app.solution_contract import (
    SOLUTION_SCHEMA_VERSION,
    ApproveSolutionRequest,
    BuildDocumentUpdateRequest,
    SolutionCaseDocument,
    SolutionGenerateRequest,
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
    return build_current_solution_document(card, case, current)


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
    if current.version != body.expected_version:
        raise HTTPException(
            status_code=409,
            detail="The Build Contract changed. Reload before saving your edit.",
        )

    next_version = int(await db.scalar(
        select(func.coalesce(func.max(SolutionVersion.version), 0)).where(
            SolutionVersion.case_id == case.id
        )
    ) or 0) + 1
    try:
        document = replace_build_document(
            build_current_solution_document(card, case, current),
            document_key,
            body.content.strip() + "\n",
            next_version=next_version,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Build document not found") from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=409,
            detail="This agent instruction file is generated from the Build Contract",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"The edited document is not structurally valid: {exc}",
        ) from exc

    now = utc_now()
    version = SolutionVersion(
        id=str(uuid.uuid4()),
        case_id=str(case.id),
        version=next_version,
        schema_version=SOLUTION_SCHEMA_VERSION,
        provider="user",
        model="manual",
        problem_case_version=document.problem_case_version,
        document=document.model_dump(mode="json", by_alias=True),
        generated_at=now,
    )
    db.add(version)
    await db.flush()
    case.current_version_id = version.id
    case.approval_status = "draft"
    case.approved_version_id = None
    case.approved_at = None
    case.updated_at = now
    await db.commit()
    return build_current_solution_document(card, case, version)


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
    if current.version != body.expected_version:
        raise HTTPException(
            status_code=409,
            detail="The Build Contract changed. Reload before approving it.",
        )

    now = utc_now()
    case.approval_status = "approved"
    case.approved_version_id = current.id
    case.approved_at = now
    case.updated_at = now
    await db.commit()
    return build_current_solution_document(card, case, current)


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
