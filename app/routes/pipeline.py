"""User pipeline cards (portfolio workspace) + their cluster-anchored Signal,
source post reader and brief views.

The pipeline *card* CRUD is owned by the API (the ``pipeline`` table). Every
read of underlying market data goes through the flat ``cluster_items`` table:
a card either points at a system cluster (``source_cluster_id``) or carries an
explicit list of ``cluster_items`` ids (``post_ids``).
"""
import csv
import io
import logging
import math
import random
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Response
from sqlalchemy import select, text, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db import get_db, AsyncSessionLocal
from app.auth import get_uid
from app.config import PIPELINE_VERSION
from app.models import Pipeline, Cluster, ClusterItem, Issue, Team, TeamMember
from app.limits import get_plan, limit_for, LimitReached
from app.routes.clusters import (
    serialize_evidence_item,
    load_cluster_items,
    distinct_solution_angles,
    representative_problem_statement,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

PATCHABLE_FIELDS = {
    "stage", "kill_criteria", "distribution_channels", "concept_angles",
    "exit_checklist", "notes", "url", "category", "revenue_model", "status", "name",
    "outcome", "mrr", "team_id",
}

# Canonical pipeline stages, in progression order. "watching" is the backlog;
# terminal states (launched / archived) are tracked via launched_at / removed_at.
PIPELINE_STAGES = ("watching", "discovery", "exploring", "validating", "building")
VALID_STAGES = frozenset(PIPELINE_STAGES)

MAX_COHERENCE_POSTS = 50
SIGNAL_BRIEF_SCAN = 500

EXPORT_CSV_COLUMNS = (
    "pipeline_id",
    "pipeline_name",
    "pipeline_stage",
    "source_cluster_id",
    "cluster_id",
    "cluster_name",
    "post_id",
    "title",
    "body",
    "community",
    "url",
    "author",
    "score",
    "num_comments",
    "posted_at",
)


def _format_cluster_metrics(raw: dict | None) -> dict | None:
    if not raw:
        return None
    return {
        "avgSourceScore": raw.get("avg_source_score"),
        "coherenceScore": raw.get("coherence_score"),
        "postCount": raw.get("post_count"),
        "computedAt": raw.get("computed_at"),
    }


def _serialize(
    p: Pipeline,
    posts: list[dict] | None = None,
    team: dict | None = None,
    open_issue_count: int = 0,
    open_kill_criteria_count: int = 0,
) -> dict:
    return {
        "id": p.id,
        "userId": p.user_id,
        "teamId": p.team_id,
        "team": team,
        "name": p.name,
        "postIds": p.post_ids or [],
        "sourceClusterId": p.source_cluster_id,
        "stage": p.stage,
        "killCriteria": p.kill_criteria,
        "distributionChannels": p.distribution_channels or [],
        "conceptAngles": p.concept_angles or [],
        "exitChecklist": p.exit_checklist,
        "notes": p.notes,
        "url": p.url,
        "category": p.category,
        "revenueModel": p.revenue_model,
        "status": p.status,
        "clusterMetrics": _format_cluster_metrics(p.cluster_metrics),
        "outcome": p.outcome,
        "mrr": p.mrr,
        "outcomeNotedAt": p.outcome_noted_at.isoformat() if p.outcome_noted_at else None,
        "launchedAt": p.launched_at.isoformat() if p.launched_at else None,
        "removedAt": p.removed_at.isoformat() if p.removed_at else None,
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
        "openIssueCount": open_issue_count,
        "openKillCriteriaCount": open_kill_criteria_count,
        "posts": posts or [],
    }


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value else None


def _avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _item_observed(it: ClusterItem):
    return it.posted_at or it.scraped_at


def _upvote_ratio(it: ClusterItem):
    return it.raw_json.get("upvote_ratio") if isinstance(it.raw_json, dict) else None


def _serialize_signal_post(it: ClusterItem) -> dict:
    return {
        "id": it.id,
        "title": it.title or "Untitled post",
        "description": it.body,
        "source": it.community or it.platform,
        "sourceUrl": it.url or it.permalink,
        "author": it.author,
        "sourceScore": it.score,
        "numComments": it.num_comments,
        "createdAt": _iso_or_none(_item_observed(it)),
        "similarityScore": it.similarity_score,
        "problemStatement": it.problem_statement,
        "solutionAngle": it.solution_angle,
    }


def _serialize_signal_cluster(cluster: Cluster | None) -> dict | None:
    if not cluster:
        return None
    return {
        "id": cluster.id,
        "name": cluster.name,
        "summary": cluster.summary,
        "trending": cluster.trending,
        "signalScore": cluster.signal_score,
    }


def _serialize_signal_cluster_workspace(cluster: Cluster, items: list[ClusterItem]) -> dict:
    dates = sorted(d for it in items if (d := _iso_or_none(_item_observed(it))))
    return {
        **_serialize_signal_cluster(cluster),
        "opportunityType": _most_common(it.opportunity_type for it in items),
        "opportunityDomain": _most_common(it.opportunity_domain for it in items),
        "postCount": len(items),
        "commentCount": sum(it.num_comments or 0 for it in items),
        "averageUpvoteRatio": _avg([_upvote_ratio(it) for it in items]),
        "authorCount": cluster.author_count,
        "communityCount": cluster.community_count,
        "dateRange": {"from": dates[0] if dates else None, "to": dates[-1] if dates else None},
    }


def _most_common(values):
    from collections import Counter
    cleaned = [v for v in values if v]
    return Counter(cleaned).most_common(1)[0][0] if cleaned else None


def _workspace_metrics(items: list[ClusterItem]) -> dict:
    return {
        "postCount": len(items),
        "avgSourceScore": _avg([it.score for it in items]),
        "commentCount": sum(it.num_comments or 0 for it in items),
        "averageUpvoteRatio": _avg([_upvote_ratio(it) for it in items]),
    }


def _cluster_id_from_card(card: Pipeline) -> int | None:
    try:
        return int(card.source_cluster_id) if card.source_cluster_id else None
    except (TypeError, ValueError):
        return None


async def _get_active_pipeline_card(pipeline_id: str, db: AsyncSession, uid: str) -> Pipeline:
    result = await db.execute(
        select(Pipeline).where(
            Pipeline.id == pipeline_id,
            Pipeline.user_id == uid,
            Pipeline.launched_at == None,
            Pipeline.removed_at == None,
        )
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Active pipeline card not found")
    return card


async def _team_visible(team_id: str, db: AsyncSession, uid: str) -> Team | None:
    team = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
    if not team:
        return None
    if team.owner_user_id == uid:
        return team
    member = (await db.execute(
        select(TeamMember).where(
            TeamMember.team_id == team_id,
            TeamMember.user_id == uid,
            TeamMember.status != "removed",
        )
    )).scalar_one_or_none()
    return team if member else None


async def _items_for_card(card: Pipeline, db: AsyncSession) -> tuple[Cluster | None, list[ClusterItem]]:
    """Resolve a card to (cluster, items): the system cluster's items when the
    card points at one, otherwise the card's explicit post_ids."""
    cluster_id = _cluster_id_from_card(card)
    cluster = None
    if cluster_id is not None:
        cluster = (await db.execute(
            select(Cluster).where(
                Cluster.id == cluster_id,
                Cluster.pipeline_version == PIPELINE_VERSION,
            )
        )).scalar_one_or_none()

    if cluster is not None:
        items = await load_cluster_items(db, cluster_id)
    elif card.post_ids:
        items = list((await db.execute(
            select(ClusterItem)
            .where(
                ClusterItem.id.in_(card.post_ids),
                ClusterItem.pipeline_version == PIPELINE_VERSION,
            )
            .order_by(ClusterItem.score.desc().nullslast())
        )).scalars().all())
    else:
        items = []
    return cluster, items


def _csv_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _build_signal_csv(card: Pipeline, cluster: Cluster | None, items: list[ClusterItem]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=EXPORT_CSV_COLUMNS)
    writer.writeheader()
    for it in items:
        writer.writerow({
            "pipeline_id": card.id,
            "pipeline_name": card.name,
            "pipeline_stage": card.stage,
            "source_cluster_id": card.source_cluster_id,
            "cluster_id": cluster.id if cluster else None,
            "cluster_name": cluster.name if cluster else None,
            "post_id": it.id,
            "title": it.title,
            "body": it.body,
            "community": it.community,
            "url": it.url or it.permalink,
            "author": it.author,
            "score": it.score,
            "num_comments": it.num_comments,
            "posted_at": _csv_value(it.posted_at),
        })
    return output.getvalue()


def _serialize_pipeline_signal_payload(
    card: Pipeline, cluster: Cluster | None, items: list[ClusterItem]
) -> dict:
    return {
        "pipeline": {
            "id": card.id,
            "name": card.name,
            "notes": card.notes,
            "sourceClusterId": card.source_cluster_id,
            "stage": card.stage,
        },
        "cluster": _serialize_signal_cluster(cluster),
        "metrics": _workspace_metrics(items),
        "posts": [_serialize_signal_post(it) for it in items],
    }


def _parse_embedding(raw) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw).strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return None
        return [float(x) for x in inner.split(",")]
    return None


async def _compute_and_write_metrics(pipeline_id: str, post_ids: list[str]) -> None:
    """Background task: compute cluster metrics (avg score + embedding coherence)
    over the card's cluster_items and write them back to the pipeline row."""
    if not post_ids:
        return

    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                select(ClusterItem.id, ClusterItem.score).where(
                    ClusterItem.id.in_(post_ids),
                    ClusterItem.pipeline_version == PIPELINE_VERSION,
                )
            )
            rows = result.all()
            if not rows:
                return

            count = len(rows)
            scores = [r.score for r in rows if r.score is not None]
            avg_source_score = round(sum(scores) / len(scores), 2) if scores else None

            coherence_score: float | None = None
            if count == 1:
                coherence_score = 1.0
            else:
                sample = post_ids if count <= MAX_COHERENCE_POSTS else random.sample(post_ids, MAX_COHERENCE_POSTS)
                emb_result = await db.execute(
                    text(
                        "SELECT id::text, embedding::text AS emb "
                        "FROM cluster_items "
                        "WHERE id::text = ANY(:ids) AND embedding IS NOT NULL "
                        "AND pipeline_version = :pipeline_version"
                    ),
                    {"ids": sample, "pipeline_version": PIPELINE_VERSION},
                )
                vecs = [v for v in (_parse_embedding(r[1]) for r in emb_result.all()) if v is not None]
                if len(vecs) >= 2:
                    magnitudes = [math.sqrt(sum(x * x for x in v)) for v in vecs]
                    similarities: list[float] = []
                    for i in range(len(vecs)):
                        for j in range(i + 1, len(vecs)):
                            mag_i, mag_j = magnitudes[i], magnitudes[j]
                            if mag_i == 0.0 or mag_j == 0.0:
                                continue
                            dot = sum(x * y for x, y in zip(vecs[i], vecs[j]))
                            similarities.append(dot / (mag_i * mag_j))
                    if similarities:
                        coherence_score = round(sum(similarities) / len(similarities), 4)
                elif len(vecs) == 1:
                    coherence_score = 1.0

            metrics = {
                "avg_source_score": avg_source_score,
                "coherence_score": coherence_score,
                "post_count": count,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            }

            pipeline = (await db.execute(select(Pipeline).where(Pipeline.id == pipeline_id))).scalar_one_or_none()
            if pipeline:
                pipeline.cluster_metrics = metrics
                await db.commit()
        except Exception:
            logger.exception("cluster_metrics computation failed for pipeline %s", pipeline_id)


async def _fetch_posts_for_cards(cards: list[Pipeline], db: AsyncSession) -> dict[str, list[dict]]:
    """Batch-fetch {id, title, sourceScore} for all post_ids across the cards."""
    all_ids = list({pid for c in cards for pid in (c.post_ids or [])})
    if not all_ids:
        return {str(c.id): [] for c in cards}

    result = await db.execute(
        select(ClusterItem.id, ClusterItem.title, ClusterItem.score)
        .where(
            ClusterItem.id.in_(all_ids),
            ClusterItem.pipeline_version == PIPELINE_VERSION,
        )
    )
    post_map: dict[str, dict] = {
        str(row.id): {"id": str(row.id), "title": row.title, "sourceScore": row.score}
        for row in result
    }
    return {
        str(c.id): [post_map[pid] for pid in (c.post_ids or []) if pid in post_map]
        for c in cards
    }


async def _fetch_teams_for_cards(cards: list[Pipeline], db: AsyncSession) -> dict[str, dict]:
    team_ids = list({str(c.team_id) for c in cards if c.team_id})
    if not team_ids:
        return {}
    teams = list((await db.execute(select(Team).where(Team.id.in_(team_ids)))).scalars().all())
    return {
        str(team.id): {
            "id": team.id,
            "name": team.name,
            "description": team.description,
        }
        for team in teams
    }


async def _fetch_issue_counts_for_cards(cards: list[Pipeline], db: AsyncSession) -> dict[str, dict[str, int]]:
    pipeline_ids = [str(c.id) for c in cards]
    if not pipeline_ids:
        return {}
    rows = (await db.execute(
        select(Issue.pipeline_id, Issue.issue_type, func.count())
        .where(
            Issue.pipeline_id.in_(pipeline_ids),
            Issue.status == "open",
            Issue.parent_issue_id == None,
        )
        .group_by(Issue.pipeline_id, Issue.issue_type)
    )).all()
    counts = {pid: {"issue": 0, "kill_criteria": 0} for pid in pipeline_ids}
    for pipeline_id, issue_type, count in rows:
        if str(pipeline_id) in counts and issue_type in counts[str(pipeline_id)]:
            counts[str(pipeline_id)][issue_type] = int(count)
    return counts


def _serialize_cards(
    cards: list[Pipeline],
    posts_map: dict[str, list[dict]],
    teams_map: dict[str, dict],
    counts_map: dict[str, dict[str, int]],
) -> list[dict]:
    return [
        _serialize(
            c,
            posts=posts_map.get(str(c.id), []),
            team=teams_map.get(str(c.team_id)) if c.team_id else None,
            open_issue_count=counts_map.get(str(c.id), {}).get("issue", 0),
            open_kill_criteria_count=counts_map.get(str(c.id), {}).get("kill_criteria", 0),
        )
        for c in cards
    ]


class CreatePipelineBody(BaseModel):
    name: str
    post_ids: list[str]
    source_cluster_id: Optional[str] = None
    team_id: Optional[str] = None


class PatchPipelineBody(BaseModel):
    stage: Optional[str] = None
    kill_criteria: Optional[str] = None
    distribution_channels: Optional[list[str]] = None
    concept_angles: Optional[list] = None
    exit_checklist: Optional[dict] = None
    notes: Optional[str] = None
    url: Optional[str] = None
    category: Optional[str] = None
    revenue_model: Optional[str] = None
    status: Optional[str] = None
    name: Optional[str] = None
    outcome: Optional[str] = None
    mrr: Optional[float] = None
    post_ids: Optional[list[str]] = None
    team_id: Optional[str] = None


class LaunchBody(BaseModel):
    product_name: str


class WatchBody(BaseModel):
    cluster_id: str


@router.get("")
async def get_pipeline(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.launched_at == None,
            Pipeline.removed_at == None,
        ).order_by(Pipeline.created_at.desc())
    )
    cards = result.scalars().all()
    posts_map = await _fetch_posts_for_cards(list(cards), db)
    teams_map = await _fetch_teams_for_cards(list(cards), db)
    counts_map = await _fetch_issue_counts_for_cards(list(cards), db)
    return {"data": _serialize_cards(list(cards), posts_map, teams_map, counts_map)}


async def _find_watch_card(uid: str, cluster_id: str, db: AsyncSession) -> Pipeline | None:
    return (await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.source_cluster_id == str(cluster_id),
            Pipeline.removed_at == None,
        )
    )).scalar_one_or_none()


@router.post("/watch")
async def watch_cluster(
    body: WatchBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Add a cluster to the user's pipeline at the Watching stage. The card is
    seeded with the cluster's cluster_items ids. Idempotent."""
    existing = await _find_watch_card(uid, body.cluster_id, db)
    if existing:
        return {"is_watched": True, "pipeline_id": str(existing.id)}

    try:
        cluster_int_id = int(body.cluster_id)
    except (TypeError, ValueError):
        cluster_int_id = None

    cluster = None
    if cluster_int_id is not None:
        cluster = (await db.execute(
            select(Cluster).where(
                Cluster.id == cluster_int_id,
                Cluster.pipeline_version == PIPELINE_VERSION,
            )
        )).scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    active_cards = (await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.launched_at == None,
            Pipeline.removed_at == None,
        )
    )).scalars().all()
    plan = await get_plan(uid, db)
    cap = limit_for(plan, "max_pipeline_cards")
    if cap is not None and len(active_cards) >= cap:
        raise LimitReached("pipeline", plan, status_code=403)

    post_id_rows = (await db.execute(
        select(ClusterItem.id).where(
            ClusterItem.cluster_id == cluster_int_id,
            ClusterItem.pipeline_version == PIPELINE_VERSION,
        )
    )).all()
    post_ids = [str(r[0]) for r in post_id_rows]

    card = Pipeline(
        user_id=uid,
        name=cluster.name or f"Cluster {body.cluster_id}",
        post_ids=post_ids,
        source_cluster_id=str(body.cluster_id),
        stage="watching",
    )
    db.add(card)
    await db.commit()
    await db.refresh(card)
    return {"is_watched": True, "pipeline_id": str(card.id)}


@router.delete("/watch/{cluster_id}")
async def unwatch_cluster(
    cluster_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _find_watch_card(uid, cluster_id, db)
    if card:
        now = datetime.now(timezone.utc)
        card.removed_at = now
        card.updated_at = now
        await db.commit()
    return {"is_watched": False}


@router.get("/{pipeline_id}/signal")
async def get_pipeline_signal(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Signal workspace anchored to an active user pipeline card."""
    card = await _get_active_pipeline_card(pipeline_id, db, uid)
    cluster, items = await _items_for_card(card, db)

    pipeline_payload = {
        "id": card.id,
        "name": card.name,
        "notes": card.notes,
        "sourceClusterId": card.source_cluster_id,
        "stage": card.stage,
    }

    if cluster:
        return {
            "pipeline": pipeline_payload,
            "cluster": _serialize_signal_cluster_workspace(cluster, items),
            "metrics": _workspace_metrics(items),
            "analytics": {
                "sourceBreakdown": cluster.source_breakdown or [],
                "postVolumeByDate": cluster.post_volume_by_date or [],
            },
            "availability": {"clusterAnalytics": True, "brief": True},
            "problemStatement": representative_problem_statement(items),
            "posts": [_serialize_signal_post(it) for it in items],
        }

    return {
        "pipeline": pipeline_payload,
        "cluster": None,
        "metrics": _workspace_metrics(items),
        "analytics": None,
        "availability": {"clusterAnalytics": False, "brief": False},
        "problemStatement": representative_problem_statement(items),
        "posts": [_serialize_signal_post(it) for it in items],
    }


def _evidence_scope(card: Pipeline, cluster_id: int | None):
    """The where-condition that scopes cluster_items to this card."""
    if cluster_id is not None:
        return ClusterItem.cluster_id == cluster_id
    return ClusterItem.id.in_(card.post_ids or [])


async def _list_pipeline_posts(
    pipeline_id: str,
    q: str | None,
    community: str | None,
    sort: Literal["engagement", "newest", "comments", "similarity"],
    page: int,
    pageSize: int,
    db: AsyncSession,
    uid: str,
):
    """Paginated, searchable source posts for a pipeline workspace."""
    card = await _get_active_pipeline_card(pipeline_id, db, uid)
    cluster_id = _cluster_id_from_card(card)
    offset = (page - 1) * pageSize

    conditions = [ClusterItem.pipeline_version == PIPELINE_VERSION, _evidence_scope(card, cluster_id)]
    if q:
        conditions.append(or_(ClusterItem.title.ilike(f"%{q}%"), ClusterItem.body.ilike(f"%{q}%")))
    if community:
        conditions.append(ClusterItem.community == community)

    order = {
        "newest": func.coalesce(ClusterItem.posted_at, ClusterItem.scraped_at).desc().nullslast(),
        "comments": ClusterItem.num_comments.desc().nullslast(),
        "similarity": ClusterItem.similarity_score.desc().nullslast(),
    }.get(sort, ClusterItem.score.desc().nullslast())

    total = await db.scalar(select(func.count()).select_from(ClusterItem).where(*conditions))
    rows = list((await db.execute(
        select(ClusterItem).where(*conditions).order_by(order).offset(offset).limit(pageSize)
    )).scalars().all())

    communities = [
        value for (value,) in (await db.execute(
            select(ClusterItem.community)
            .where(
                ClusterItem.pipeline_version == PIPELINE_VERSION,
                _evidence_scope(card, cluster_id),
                ClusterItem.community.isnot(None),
            )
            .distinct()
            .order_by(ClusterItem.community)
        )).all()
        if value
    ]

    return {
        "data": [serialize_evidence_item(it) for it in rows],
        "total": total or 0,
        "page": page,
        "pageSize": pageSize,
        "communities": communities,
    }


async def _get_pipeline_post(
    pipeline_id: str,
    post_id: str,
    db: AsyncSession,
    uid: str,
):
    """Full source post after verifying it belongs to the pipeline workspace."""
    card = await _get_active_pipeline_card(pipeline_id, db, uid)
    cluster_id = _cluster_id_from_card(card)

    item = None
    if cluster_id is not None:
        item = (await db.execute(
            select(ClusterItem).where(
                ClusterItem.id == post_id,
                ClusterItem.cluster_id == cluster_id,
                ClusterItem.pipeline_version == PIPELINE_VERSION,
            )
        )).scalar_one_or_none()
    elif post_id in (card.post_ids or []):
        item = (await db.execute(
            select(ClusterItem).where(
                ClusterItem.id == post_id,
                ClusterItem.pipeline_version == PIPELINE_VERSION,
            )
        )).scalar_one_or_none()

    if not item:
        raise HTTPException(status_code=404, detail="Source post not found")
    return serialize_evidence_item(item)


@router.get("/{pipeline_id}/posts")
async def get_pipeline_posts(
    pipeline_id: str,
    q: str | None = Query(None),
    community: str | None = Query(None),
    sort: Literal["engagement", "newest", "comments", "similarity"] = Query("engagement"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    return await _list_pipeline_posts(pipeline_id, q, community, sort, page, pageSize, db, uid)


@router.get("/{pipeline_id}/posts/{post_id}")
async def get_pipeline_post_detail(
    pipeline_id: str,
    post_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    return await _get_pipeline_post(pipeline_id, post_id, db, uid)


@router.get("/{pipeline_id}/signal/evidence")
async def get_pipeline_signal_evidence(
    pipeline_id: str,
    q: str | None = Query(None),
    community: str | None = Query(None),
    sort: Literal["engagement", "newest", "comments", "similarity"] = Query("engagement"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Compatibility alias for the old Signal Evidence tab."""
    return await _list_pipeline_posts(pipeline_id, q, community, sort, page, pageSize, db, uid)


@router.get("/{pipeline_id}/signal/evidence/{post_id}")
async def get_pipeline_signal_evidence_detail(
    pipeline_id: str,
    post_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Compatibility alias for the old Signal Evidence detail endpoint."""
    return await _get_pipeline_post(pipeline_id, post_id, db, uid)


@router.get("/{pipeline_id}/signal/brief")
async def get_pipeline_signal_brief(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Problem/solution brief for the card's cluster, derived from the
    pre-computed per-item problem_statement / solution_angle."""
    card = await _get_active_pipeline_card(pipeline_id, db, uid)
    cluster_id = _cluster_id_from_card(card)
    if cluster_id is None:
        return {"available": False, "reason": "Cluster analytics are unavailable for this pipeline card."}

    items = await load_cluster_items(db, cluster_id, limit=SIGNAL_BRIEF_SCAN)
    return {
        "available": True,
        "clusterId": cluster_id,
        "problemStatement": representative_problem_statement(items),
        "solutionAngles": distinct_solution_angles(items),
        "postCount": len(items),
    }


@router.get("/{pipeline_id}/export")
async def export_pipeline_signal(
    pipeline_id: str,
    export_format: Literal["csv", "json"] = Query("csv", alias="format"),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Export the signal dataset backing the Signal workspace."""
    card = (await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    cluster, items = await _items_for_card(card, db)

    if export_format == "json":
        return _serialize_pipeline_signal_payload(card, cluster, items)

    csv_body = _build_signal_csv(card, cluster, items)
    return Response(
        content=csv_body,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="problemfinder-{pipeline_id}-signals.csv"',
        },
    )


@router.post("")
async def create_pipeline(
    body: CreatePipelineBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    if not body.post_ids:
        raise HTTPException(status_code=400, detail="post_ids must not be empty")
    if body.team_id and not await _team_visible(body.team_id, db, uid):
        raise HTTPException(status_code=404, detail="Team not found")

    existing_result = await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.launched_at == None,
            Pipeline.removed_at == None,
        )
    )
    active_cards = existing_result.scalars().all()

    plan = await get_plan(uid, db)
    cap = limit_for(plan, "max_pipeline_cards")
    if cap is not None and len(active_cards) >= cap:
        raise LimitReached("pipeline", plan, status_code=403)

    new_ids_set = set(body.post_ids)
    for card in active_cards:
        if new_ids_set.intersection(card.post_ids or []):
            raise HTTPException(
                status_code=409,
                detail={"error": "DUPLICATE_POST_IDS", "existingClusterId": card.id},
            )

    card = Pipeline(
        user_id=uid,
        team_id=body.team_id,
        name=body.name,
        post_ids=body.post_ids,
        source_cluster_id=body.source_cluster_id,
    )
    db.add(card)
    await db.commit()
    await db.refresh(card)

    background_tasks.add_task(_compute_and_write_metrics, str(card.id), list(body.post_ids))

    posts_map = await _fetch_posts_for_cards([card], db)
    teams_map = await _fetch_teams_for_cards([card], db)
    counts_map = await _fetch_issue_counts_for_cards([card], db)
    return {"data": _serialize_cards([card], posts_map, teams_map, counts_map)[0]}


@router.patch("/{pipeline_id}")
async def patch_pipeline(
    pipeline_id: str,
    body: PatchPipelineBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    updates = body.model_dump(exclude_none=True)
    if "team_id" in body.model_fields_set:
        updates["team_id"] = body.team_id

    if "stage" in updates and updates["stage"] not in VALID_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stage '{updates['stage']}'. Must be one of: {', '.join(PIPELINE_STAGES)}",
        )
    if "team_id" in updates and updates["team_id"] and not await _team_visible(updates["team_id"], db, uid):
        raise HTTPException(status_code=404, detail="Team not found")

    post_ids_changed = "post_ids" in updates

    for field, value in updates.items():
        setattr(card, field, value)
    if "outcome" in updates or "mrr" in updates:
        card.outcome_noted_at = datetime.now(timezone.utc)
    card.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(card)

    if post_ids_changed:
        background_tasks.add_task(_compute_and_write_metrics, str(card.id), list(card.post_ids or []))

    posts_map = await _fetch_posts_for_cards([card], db)
    teams_map = await _fetch_teams_for_cards([card], db)
    counts_map = await _fetch_issue_counts_for_cards([card], db)
    return {"data": _serialize_cards([card], posts_map, teams_map, counts_map)[0]}


@router.post("/{pipeline_id}/launch")
async def launch_pipeline(
    pipeline_id: str,
    body: LaunchBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")
    if card.launched_at:
        raise HTTPException(status_code=409, detail="Already launched")

    card.launched_at = datetime.now(timezone.utc)
    card.name = body.product_name
    card.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(card)
    return {"data": _serialize(card)}


@router.delete("/{pipeline_id}")
async def delete_pipeline(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    card.removed_at = datetime.now(timezone.utc)
    card.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return {"success": True}
