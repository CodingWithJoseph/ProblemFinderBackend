import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db import get_db, AsyncSessionLocal
from app.auth import get_uid
from app.config import PIPELINE_VERSION
from app.models import Problem, Pipeline, ClusterItem
from app.openai_client import openai_client, EMBEDDING_MODEL
from app.routes.clusters import serialize_evidence_item

router = APIRouter(prefix="/problems", tags=["problems"])

logger = logging.getLogger(__name__)

SIMILAR_THRESHOLD = 0.85
DEFAULT_SIMILAR_POSTS = 10
MAX_SIMILAR_POSTS = 25


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _serialize(p: Problem) -> dict:
    return {
        "id": p.id,
        "pipelineId": p.pipeline_id,
        "userId": p.user_id,
        "title": p.title,
        "description": p.description,
        "sourcePostId": p.source_post_id,
        "position": p.position,
        "embeddingReady": p.embedding is not None,
        "painLevel": p.pain_level,
        "urgencyLevel": p.urgency_level,
        "frequencyScore": p.frequency_score,
        "currentAlternatives": p.current_alternatives,
        "buyerType": p.buyer_type,
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
    }


def _embedding_text(title: str, description: str | None) -> str:
    return f"{title}\n\n{description}".strip() if description else title


def _to_list(emb) -> list[float] | None:
    if emb is None:
        return None
    try:
        return [float(x) for x in emb]
    except TypeError:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    if ma == 0.0 or mb == 0.0:
        return 0.0
    return dot / (ma * mb)


async def _embed_problem(problem_id: str) -> None:
    """Background task: compute and store the OpenAI embedding for a problem."""
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(select(Problem).where(Problem.id == problem_id))
            problem = result.scalar_one_or_none()
            if not problem or not problem.title:
                return
            response = await openai_client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=_embedding_text(problem.title, problem.description),
            )
            problem.embedding = response.data[0].embedding
            problem.updated_at = datetime.now(timezone.utc)
            await db.commit()
        except Exception:
            # Background task — never surface to the request, but log it.
            logger.exception("problem embedding computation failed")


class CreateProblemBody(BaseModel):
    pipeline_id: str
    title: str
    description: Optional[str] = None
    source_post_id: Optional[str] = None


class PatchProblemBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    position: Optional[int] = None
    pain_level: Optional[str] = None
    urgency_level: Optional[str] = None
    frequency_score: Optional[float] = None
    current_alternatives: Optional[str] = None
    buyer_type: Optional[str] = None


@router.get("")
async def list_problems(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Problem)
        .where(Problem.pipeline_id == pipeline_id, Problem.user_id == uid)
        .order_by(Problem.position.asc(), Problem.created_at.asc())
    )
    problems = result.scalars().all()
    return {"data": [_serialize(p) for p in problems]}


@router.get("/similar")
async def similar_problems(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Find semantically similar problem pairs within a pipeline (duplicate/overlap)."""
    result = await db.execute(
        select(Problem).where(Problem.pipeline_id == pipeline_id, Problem.user_id == uid)
    )
    problems = result.scalars().all()

    with_emb = []
    for p in problems:
        emb = _to_list(p.embedding)
        if emb:
            with_emb.append((p, emb))

    pairs = []
    for i in range(len(with_emb)):
        for j in range(i + 1, len(with_emb)):
            pa, ea = with_emb[i]
            pb, eb = with_emb[j]
            sim = _cosine(ea, eb)
            if sim >= SIMILAR_THRESHOLD:
                pairs.append({
                    "a": pa.id,
                    "aTitle": pa.title,
                    "b": pb.id,
                    "bTitle": pb.title,
                    "similarity": round(sim, 4),
                })

    pairs.sort(key=lambda x: x["similarity"], reverse=True)
    return {"data": pairs}


@router.get("/{problem_id}/similar-posts")
async def similar_posts(
    problem_id: str,
    limit: int = Query(DEFAULT_SIMILAR_POSTS, ge=1, le=MAX_SIMILAR_POSTS),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """cluster_items related to this problem.

    The pipeline's per-item embeddings are 3072-d (text-embedding-3-large) while
    problem embeddings are 1536-d (text-embedding-3-small), so the two spaces are
    not directly comparable. We surface related posts by keyword-matching the
    problem's title against cluster_items, ranked by reddit score.
    """
    result = await db.execute(
        select(Problem).where(Problem.id == problem_id, Problem.user_id == uid)
    )
    problem = result.scalar_one_or_none()
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")

    text_q = (problem.title or "").strip()
    tokens = [t.strip() for t in text_q.split() if len(t.strip()) >= 4][:6]
    search_terms = tokens or ([text_q] if text_q else [])
    if not search_terms:
        return {"data": []}

    ors = []
    for term in search_terms:
        ors.append(ClusterItem.title.ilike(f"%{term}%"))
        ors.append(ClusterItem.body.ilike(f"%{term}%"))

    rows = await db.execute(
        select(ClusterItem)
        .where(ClusterItem.pipeline_version == PIPELINE_VERSION, or_(*ors))
        .order_by(ClusterItem.score.desc().nullslast())
        .limit(limit)
    )
    items = rows.scalars().all()
    return {"data": [serialize_evidence_item(it) for it in items]}


@router.post("")
async def create_problem(
    body: CreateProblemBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    # Verify the caller owns the target pipeline before attaching anything to
    # it (prevents writing problems under another user's pipeline_id).
    owns = (await db.execute(
        select(Pipeline.id).where(
            Pipeline.id == body.pipeline_id, Pipeline.user_id == uid
        )
    )).scalar_one_or_none()
    if owns is None:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    problem = Problem(
        pipeline_id=body.pipeline_id,
        user_id=uid,
        title=body.title,
        description=body.description,
        source_post_id=body.source_post_id,
    )

    db.add(problem)
    await db.commit()
    await db.refresh(problem)

    # Compute the embedding out-of-band so the create returns instantly.
    background_tasks.add_task(_embed_problem, str(problem.id))

    return {"data": _serialize(problem)}


@router.patch("/{problem_id}")
async def patch_problem(
    problem_id: str,
    body: PatchProblemBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Problem).where(Problem.id == problem_id, Problem.user_id == uid)
    )
    problem = result.scalar_one_or_none()
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")

    updates = body.model_dump(exclude_none=True)
    content_changed = "title" in updates or "description" in updates
    for field, value in updates.items():
        setattr(problem, field, value)
    problem.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(problem)

    # Title/description changed -> embedding is stale; recompute in background.
    if content_changed:
        background_tasks.add_task(_embed_problem, str(problem.id))

    return {"data": _serialize(problem)}


@router.delete("/{problem_id}")
async def delete_problem(
    problem_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Problem).where(Problem.id == problem_id, Problem.user_id == uid)
    )
    problem = result.scalar_one_or_none()
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")

    await db.delete(problem)
    await db.commit()
    return {"success": True}
