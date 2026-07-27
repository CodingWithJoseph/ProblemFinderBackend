"""Versioned Signal case read and refresh endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.db import get_db
from app.routes.pipeline import _get_pipeline_card
from app.services.signal_cases import (
    build_current_case_document,
    build_empty_case_document,
    find_current_version,
    find_signal_case,
    persist_new_signal_case,
    queue_signal_refresh,
)
from app.signal_contract import SignalCaseDocument


router = APIRouter(prefix="/pipeline", tags=["signal"])


@router.get(
    "/{pipeline_id}/signal/case",
    response_model=SignalCaseDocument,
)
async def get_signal_case(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_signal_case(db, pipeline_id, uid)
    if case is None:
        # Existing Pipeline projects predate Signal cases. Their first read
        # performs a one-time initialization; later reads are side-effect free.
        case = await persist_new_signal_case(db, card, uid)

    current = await find_current_version(db, case)
    if current is None:
        return build_empty_case_document(card, case)

    try:
        return build_current_case_document(card, case, current)
    except ValueError as exc:
        # Never return an unvalidated generated document to the client.
        raise HTTPException(
            status_code=503,
            detail="The saved Signal analysis could not be validated.",
        ) from exc


@router.post(
    "/{pipeline_id}/signal/case/refresh",
    response_model=SignalCaseDocument,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_signal_case(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_signal_case(db, pipeline_id, uid)
    if case is None:
        case = await persist_new_signal_case(db, card, uid)
    else:
        await queue_signal_refresh(db, case, uid)

    current = await find_current_version(db, case)
    if current is None:
        return build_empty_case_document(card, case)
    return build_current_case_document(card, case, current)

