"""Versioned Signal case, overrides and grounded conversation endpoints."""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from app.auth import get_uid
from app.config import get_settings
from app.db import get_db
from app.models import SignalConversation, SignalConversationTurn
from app.prompts.signal_conversation import SIGNAL_CONVERSATION_INSTRUCTIONS
from app.routes.pipeline import _get_pipeline_card
from app.services.signal_analysis_provider import (
    SignalAnalysisProviderFailure,
    get_signal_analysis_provider,
)
from app.services.signal_analysis_worker import SAFE_FAILURES, load_signal_source_items
from app.services.signal_cases import (
    build_current_case_document,
    build_empty_case_document,
    apply_signal_overrides,
    find_current_version,
    find_signal_case,
    load_signal_overrides,
    persist_new_signal_case,
    queue_signal_refresh,
    signal_source_fingerprint,
    signal_source_updated_at,
    signal_object_exists,
    upsert_signal_override,
    validate_signal_override_patch,
)
from app.signal_contract import (
    SignalCaseDocument,
    SignalConversationModelOutput,
    SignalConversationResponse,
    SignalConversationSummary,
    SignalConversationTurnResponse,
)


router = APIRouter(prefix="/pipeline", tags=["signal"])


class SignalOverrideBody(BaseModel):
    patch: dict = Field(min_length=1)


class SignalConversationCreateBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)


class SignalConversationMessageBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class SignalConversationUpdateBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    archived: bool | None = None


class SignalProposalDecisionBody(BaseModel):
    status: Literal["accepted", "rejected"]


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
        if case.status == "ready":
            items = await load_signal_source_items(db, card)
            current_fingerprint = signal_source_fingerprint(items)
            if current_fingerprint != (case.source_fingerprint or current.source_fingerprint):
                case.status = "stale"
                case.source_updated_at = signal_source_updated_at(items)
                case.updated_at = _now()
                await db.commit()
        document = build_current_case_document(card, case, current)
        overrides = await load_signal_overrides(db, case.id, uid)
        return apply_signal_overrides(document, overrides)
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
    document = build_current_case_document(card, case, current)
    overrides = await load_signal_overrides(db, case.id, uid)
    return apply_signal_overrides(document, overrides)


@router.patch(
    "/{pipeline_id}/signal/case/overrides/{object_kind}/{object_id}",
    response_model=SignalCaseDocument,
)
async def update_signal_override(
    pipeline_id: str,
    object_kind: str,
    object_id: str,
    body: SignalOverrideBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_signal_case(db, pipeline_id, uid)
    if case is None:
        raise HTTPException(status_code=409, detail="Signal analysis has not started")
    current = await find_current_version(db, case)
    if current is None:
        raise HTTPException(status_code=409, detail="Signal analysis is not ready")
    document = build_current_case_document(card, case, current)
    if not signal_object_exists(document, object_kind, object_id):
        raise HTTPException(status_code=404, detail="Signal object not found")
    try:
        validate_signal_override_patch(object_kind, body.patch)
        await upsert_signal_override(
            db,
            case,
            uid,
            object_kind,
            object_id,
            body.patch,
        )
        overrides = await load_signal_overrides(db, case.id, uid)
        return apply_signal_overrides(document, overrides)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/{pipeline_id}/signal/conversations",
    response_model=list[SignalConversationSummary],
)
async def list_signal_conversations(
    pipeline_id: str,
    include_archived: bool = False,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_signal_case(db, pipeline_id, uid)
    if case is None:
        return []
    statement = select(SignalConversation).where(
        SignalConversation.case_id == case.id,
        SignalConversation.user_id == uid,
    )
    if not include_archived:
        statement = statement.where(SignalConversation.archived_at == None)
    rows = (await db.execute(
        statement.order_by(SignalConversation.updated_at.desc())
    )).scalars().all()
    return [_conversation_summary(row) for row in rows]


@router.post(
    "/{pipeline_id}/signal/conversations",
    response_model=SignalConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_signal_conversation(
    pipeline_id: str,
    body: SignalConversationCreateBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_signal_case(db, pipeline_id, uid)
    if case is None or case.current_version_id is None:
        raise HTTPException(status_code=409, detail="Signal analysis is not ready")
    now = _now()
    conversation = SignalConversation(
        id=str(uuid.uuid4()),
        case_id=case.id,
        user_id=uid,
        title=body.title or "Signal conversation",
        created_at=now,
        updated_at=now,
    )
    db.add(conversation)
    await db.commit()
    return SignalConversationResponse(
        id=conversation.id,
        title=conversation.title,
        turns=[],
    )


@router.get(
    "/{pipeline_id}/signal/conversations/{conversation_id}",
    response_model=SignalConversationResponse,
)
async def get_signal_conversation(
    pipeline_id: str,
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_signal_case(db, pipeline_id, uid)
    conversation = await _owned_conversation(db, case, conversation_id, uid)
    turns = await _conversation_turns(db, conversation.id)
    return _conversation_response(conversation, turns)


@router.patch(
    "/{pipeline_id}/signal/conversations/{conversation_id}",
    response_model=SignalConversationSummary,
)
async def update_signal_conversation(
    pipeline_id: str,
    conversation_id: str,
    body: SignalConversationUpdateBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_signal_case(db, pipeline_id, uid)
    conversation = await _owned_conversation(db, case, conversation_id, uid)
    if body.title is not None:
        conversation.title = body.title.strip()
    if body.archived is not None:
        conversation.archived_at = _now() if body.archived else None
    conversation.updated_at = _now()
    await db.commit()
    return _conversation_summary(conversation)


@router.post(
    "/{pipeline_id}/signal/conversations/{conversation_id}/messages",
    response_model=SignalConversationResponse,
)
async def ask_signal(
    pipeline_id: str,
    conversation_id: str,
    body: SignalConversationMessageBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_signal_case(db, pipeline_id, uid)
    if case is None:
        raise HTTPException(status_code=409, detail="Signal analysis is not ready")
    current = await find_current_version(db, case)
    if current is None:
        raise HTTPException(status_code=409, detail="Signal analysis is not ready")
    conversation = await _owned_conversation(db, case, conversation_id, uid)
    turns = await _conversation_turns(db, conversation.id)
    document = build_current_case_document(card, case, current)
    overrides = await load_signal_overrides(db, case.id, uid)
    document = apply_signal_overrides(document, overrides)

    now = _now()
    user_turn = SignalConversationTurn(
        id=str(uuid.uuid4()),
        conversation_id=conversation.id,
        role="user",
        text=body.message.strip(),
        citations=[],
        proposal=None,
        insufficient_evidence=False,
        created_at=now,
    )
    db.add(user_turn)
    if conversation.title == "Signal conversation":
        conversation.title = _conversation_title(body.message)
    conversation.updated_at = now
    await db.commit()

    assistant_output = await _ask_signal_model(document, turns, body.message)
    assistant_turn = SignalConversationTurn(
        id=str(uuid.uuid4()),
        conversation_id=conversation.id,
        role="assistant",
        text=assistant_output.text,
        citations=[
            citation.model_dump(mode="json", by_alias=True)
            for citation in assistant_output.citations
        ],
        proposal=(
            assistant_output.proposal.model_dump(mode="json", by_alias=True)
            if assistant_output.proposal
            else None
        ),
        insufficient_evidence=assistant_output.insufficient_evidence,
        created_at=_now(),
    )
    db.add(assistant_turn)
    conversation.updated_at = assistant_turn.created_at
    await db.commit()
    return _conversation_response(
        conversation,
        [*turns, user_turn, assistant_turn],
    )


@router.patch(
    "/{pipeline_id}/signal/conversations/{conversation_id}/proposals/{proposal_id}",
    response_model=SignalConversationResponse,
)
async def decide_signal_proposal(
    pipeline_id: str,
    conversation_id: str,
    proposal_id: str,
    body: SignalProposalDecisionBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_pipeline_card(pipeline_id, db, uid)
    case = await find_signal_case(db, pipeline_id, uid)
    conversation = await _owned_conversation(db, case, conversation_id, uid)
    turns = await _conversation_turns(db, conversation.id)
    proposal_turn = next(
        (
            turn
            for turn in turns
            if isinstance(turn.proposal, dict)
            and str(turn.proposal.get("id")) == proposal_id
        ),
        None,
    )
    if proposal_turn is None:
        raise HTTPException(status_code=404, detail="Signal proposal not found")
    if proposal_turn.proposal.get("status") != "pending":
        raise HTTPException(status_code=409, detail="Signal proposal was already decided")

    proposal = {**proposal_turn.proposal, "status": body.status}
    if body.status == "accepted":
        target_kind = proposal.get("targetKind")
        target_id = proposal.get("targetId")
        changes = proposal.get("changes") or {}
        if target_kind and target_id and changes:
            current = await find_current_version(db, case)
            if current is None:
                raise HTTPException(status_code=409, detail="Signal analysis is not ready")
            document = build_current_case_document(card, case, current)
            if not signal_object_exists(document, target_kind, target_id):
                raise HTTPException(status_code=409, detail="Proposal target no longer exists")
            try:
                await upsert_signal_override(
                    db,
                    case,
                    uid,
                    target_kind,
                    target_id,
                    changes,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    proposal_turn.proposal = proposal
    conversation.updated_at = _now()
    await db.commit()
    return _conversation_response(conversation, turns)


async def _ask_signal_model(
    document: SignalCaseDocument,
    turns: list[SignalConversationTurn],
    message: str,
) -> SignalConversationModelOutput:
    evidence_ids = {record.id for record in document.evidence}
    try:
        provider = get_signal_analysis_provider(get_settings())
        output = await provider.ask(
            instructions=SIGNAL_CONVERSATION_INSTRUCTIONS,
            payload={
                "case": document.model_dump(mode="json", by_alias=True),
                "conversation": [
                    {"role": turn.role, "text": turn.text}
                    for turn in turns[-20:]
                ],
                "userMessage": message,
            },
        )
        cited = {citation.evidence_id for citation in output.citations}
        if output.proposal:
            cited.update(output.proposal.evidence_ids)
        unknown = cited - evidence_ids
        if unknown:
            raise SignalAnalysisProviderFailure("schema_invalid")
        return output
    except SignalAnalysisProviderFailure as exc:
        return SignalConversationModelOutput(
            text=SAFE_FAILURES.get(exc.category, SAFE_FAILURES["provider_error"]),
            citations=[],
            proposal=None,
            insufficient_evidence=True,
        )


async def _owned_conversation(
    db: AsyncSession,
    case,
    conversation_id: str,
    uid: str,
) -> SignalConversation:
    if case is None:
        raise HTTPException(status_code=404, detail="Signal conversation not found")
    conversation = (await db.execute(
        select(SignalConversation).where(
            SignalConversation.id == conversation_id,
            SignalConversation.case_id == case.id,
            SignalConversation.user_id == uid,
        )
    )).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status_code=404, detail="Signal conversation not found")
    return conversation


async def _conversation_turns(
    db: AsyncSession,
    conversation_id: str,
) -> list[SignalConversationTurn]:
    return list((await db.execute(
        select(SignalConversationTurn)
        .where(SignalConversationTurn.conversation_id == conversation_id)
        .order_by(SignalConversationTurn.created_at)
    )).scalars().all())


def _conversation_summary(conversation: SignalConversation) -> SignalConversationSummary:
    return SignalConversationSummary(
        id=conversation.id,
        title=conversation.title,
        updated_at=conversation.updated_at.isoformat(),
        archived=conversation.archived_at is not None,
    )


def _conversation_response(
    conversation: SignalConversation,
    turns: list[SignalConversationTurn],
) -> SignalConversationResponse:
    return SignalConversationResponse(
        id=conversation.id,
        title=conversation.title,
        turns=[
            SignalConversationTurnResponse(
                id=turn.id,
                role=turn.role,
                text=turn.text,
                created_at=turn.created_at.isoformat(),
                citations=turn.citations or [],
                proposal=turn.proposal,
                insufficient_evidence=turn.insufficient_evidence,
            )
            for turn in turns
        ],
    )


def _conversation_title(message: str) -> str:
    return " ".join(message.split())[:80] or "Signal conversation"


def _now() -> datetime:
    return datetime.now(timezone.utc)
