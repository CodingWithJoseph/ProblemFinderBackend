"""Owned, lightweight persistence for conversational Search sessions."""

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.db import get_db
from app.models import SearchRun, SearchSession, SearchTurn
from app.search_history import (
    SearchRunCreate,
    SearchRunResponse,
    SearchSessionCreate,
    SearchSessionDetail,
    SearchSessionSummary,
    SearchSessionUpdate,
    SearchSessionView,
    SearchTurnCreate,
    SearchTurnResponse,
)


router = APIRouter(prefix="/clusters/search/sessions", tags=["search"])
UNSAVED_RETENTION_DAYS = 30
DEFAULT_SESSION_TITLE = "New search"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expiry(now: datetime) -> datetime:
    return now + timedelta(days=UNSAVED_RETENTION_DAYS)


def _derived_title(message: str) -> str:
    compact = " ".join(message.split())
    return compact[:80] or DEFAULT_SESSION_TITLE


def _summary(session: SearchSession) -> SearchSessionSummary:
    return SearchSessionSummary(
        id=session.id,
        title=session.title,
        saved=session.saved_at is not None,
        archived=session.archived_at is not None,
        expires_at=session.expires_at,
        last_activity_at=session.last_activity_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _turn_response(turn: SearchTurn) -> SearchTurnResponse:
    return SearchTurnResponse(
        id=turn.id,
        user_message=turn.user_message,
        interpretation=turn.interpretation,
        created_at=turn.created_at,
    )


def _run_response(run: SearchRun) -> SearchRunResponse:
    return SearchRunResponse(
        id=run.id,
        draft=run.draft,
        result_cluster_ids=list(run.result_cluster_ids or []),
        result_count=run.result_count,
        created_at=run.created_at,
    )


async def _purge_expired(db: AsyncSession, now: datetime) -> None:
    await db.execute(
        delete(SearchSession).where(
            SearchSession.saved_at.is_(None),
            SearchSession.expires_at.is_not(None),
            SearchSession.expires_at <= now,
        )
    )
    await db.commit()


async def _owned_session(
    db: AsyncSession,
    uid: str,
    session_id: str,
    now: datetime,
) -> SearchSession:
    session = await db.scalar(
        select(SearchSession).where(
            SearchSession.id == session_id,
            SearchSession.user_id == uid,
            or_(
                SearchSession.saved_at.is_not(None),
                SearchSession.expires_at.is_(None),
                SearchSession.expires_at > now,
            ),
        )
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Search session not found")
    return session


def _touch(session: SearchSession, now: datetime) -> None:
    session.last_activity_at = now
    session.updated_at = now
    if session.saved_at is None:
        session.expires_at = _expiry(now)


@router.post("", response_model=SearchSessionSummary, status_code=status.HTTP_201_CREATED)
async def create_search_session(
    body: SearchSessionCreate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    now = _now()
    session = SearchSession(
        id=str(uuid.uuid4()),
        user_id=uid,
        title=body.title or DEFAULT_SESSION_TITLE,
        saved_at=None,
        archived_at=None,
        expires_at=_expiry(now),
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    await db.commit()
    return _summary(session)


@router.get("", response_model=list[SearchSessionSummary])
async def list_search_sessions(
    view: SearchSessionView = Query("recent"),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    now = _now()
    await _purge_expired(db, now)

    statement = select(SearchSession).where(SearchSession.user_id == uid)
    if view == "saved":
        statement = statement.where(
            SearchSession.saved_at.is_not(None),
            SearchSession.archived_at.is_(None),
        )
    elif view == "archived":
        statement = statement.where(SearchSession.archived_at.is_not(None))
    else:
        statement = statement.where(
            SearchSession.archived_at.is_(None),
            or_(SearchSession.saved_at.is_not(None), SearchSession.expires_at > now),
        )

    result = await db.execute(statement.order_by(SearchSession.last_activity_at.desc()).limit(limit))
    return [_summary(session) for session in result.scalars().all()]


@router.get("/{session_id}", response_model=SearchSessionDetail)
async def get_search_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    now = _now()
    session = await _owned_session(db, uid, session_id, now)
    turns_result = await db.execute(
        select(SearchTurn)
        .where(SearchTurn.session_id == session.id)
        .order_by(SearchTurn.created_at.asc())
    )
    runs_result = await db.execute(
        select(SearchRun)
        .where(SearchRun.session_id == session.id)
        .order_by(SearchRun.created_at.asc())
    )
    summary = _summary(session)
    return SearchSessionDetail(
        **summary.model_dump(),
        turns=[_turn_response(turn) for turn in turns_result.scalars().all()],
        runs=[_run_response(run) for run in runs_result.scalars().all()],
    )


@router.patch("/{session_id}", response_model=SearchSessionSummary)
async def update_search_session(
    session_id: str,
    body: SearchSessionUpdate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    now = _now()
    session = await _owned_session(db, uid, session_id, now)
    fields = body.model_fields_set

    if "title" in fields and body.title is not None:
        session.title = body.title
    if "saved" in fields and body.saved is not None:
        session.saved_at = now if body.saved else None
        session.expires_at = None if body.saved else _expiry(now)
    if "archived" in fields and body.archived is not None:
        session.archived_at = now if body.archived else None

    _touch(session, now)
    await db.commit()
    return _summary(session)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_search_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    session = await _owned_session(db, uid, session_id, _now())
    await db.delete(session)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{session_id}/turns", response_model=SearchTurnResponse, status_code=status.HTTP_201_CREATED)
async def add_search_turn(
    session_id: str,
    body: SearchTurnCreate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    now = _now()
    session = await _owned_session(db, uid, session_id, now)
    turn = SearchTurn(
        id=str(uuid.uuid4()),
        session_id=session.id,
        user_message=body.user_message,
        interpretation=body.interpretation.model_dump(mode="json"),
        created_at=now,
    )
    db.add(turn)
    if session.title == DEFAULT_SESSION_TITLE:
        session.title = _derived_title(body.user_message)
    _touch(session, now)
    await db.commit()
    return _turn_response(turn)


@router.post("/{session_id}/runs", response_model=SearchRunResponse, status_code=status.HTTP_201_CREATED)
async def add_search_run(
    session_id: str,
    body: SearchRunCreate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    now = _now()
    session = await _owned_session(db, uid, session_id, now)
    run = SearchRun(
        id=str(uuid.uuid4()),
        session_id=session.id,
        draft=body.draft.model_dump(mode="json"),
        result_cluster_ids=body.result_cluster_ids,
        result_count=body.result_count,
        created_at=now,
    )
    db.add(run)
    _touch(session, now)
    await db.commit()
    return _run_response(run)
