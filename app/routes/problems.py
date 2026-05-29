import math
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db import get_db, AsyncSessionLocal
from app.auth import get_uid
from app.models import Problem, Opportunity
from app.openai_client import openai_client, EMBEDDING_MODEL
from app.routes.opportunities import _serialize as _serialize_opportunity

router = APIRouter(prefix="/problems", tags=["problems"])

SIMILAR_THRESHOLD = 0.85
DEFAULT_SIMILAR_POSTS = 10
MAX_SIMILAR_POSTS = 25


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
            pass  # Background task - never surface errors to the request


class CreateProblemBody(BaseModel):
    pipeline_id: str
    title: str
    description: Optional[str] = None
    source_post_id: Optional[str] = None


class PatchProblemBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    position: Optional[int] = None


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
    """Top opportunities by embedding distance to this problem's embedding."""
    result = await db.execute(
        select(Problem).where(Problem.id == problem_id, Problem.user_id == uid)
    )
    problem = result.scalar_one_or_none()
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")

    emb = _to_list(problem.embedding)
    if not emb:
        return {"data": [], "embeddingReady": False}

    vector_str = "[" + ",".join(str(x) for x in emb) + "]"
    rows = await db.execute(
        text("""
            SELECT *, 1 - (embedding <=> CAST(:vec AS vector)) AS similarity
            FROM opportunities
            WHERE embedding IS NOT NULL AND deleted_at IS NULL
            ORDER BY embedding <=> CAST(:vec AS vector)
            LIMIT :limit
        """),
        {"vec": vector_str, "limit": limit},
    )
    mapped = rows.mappings().all()
    EXCLUDE_COLS = {"embedding", "search_vector", "similarity"}
    data = []
    for row in mapped:
        d = dict(row)
        sim = d.get("similarity")
        opp = Opportunity(**{k: v for k, v in d.items() if k not in EXCLUDE_COLS})
        data.append({
            **_serialize_opportunity(opp),
            "similarity": round(sim, 4) if sim is not None else None,
        })

    return {"data": data, "embeddingReady": True}


@router.post("")
async def create_problem(
    body: CreateProblemBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
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
