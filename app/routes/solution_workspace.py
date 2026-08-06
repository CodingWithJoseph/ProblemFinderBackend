"""MVP Build Warroom endpoints backed by immutable Solution Case versions."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.db import get_db
from app.routes.pipeline import _get_pipeline_card
from app.services.signal_cases import find_current_version, find_signal_case
from app.services.solution_cases import (
    build_current_solution_document,
    build_empty_solution_document,
    find_current_solution_version,
    find_solution_case,
    new_solution_case_and_job,
    queue_solution_generation,
)
from app.solution_contract import SolutionCaseDocument, SolutionGenerateRequest


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
            detail="Finish the Problem Breakdown before generating an MVP solution",
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
