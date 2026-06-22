from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.auth import get_uid
from app.config import PIPELINE_VERSION
from app.models import Pipeline, Problem, Task, Cluster, ClusterItem, ClusterSignal

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _now() -> datetime:
    return datetime.now(timezone.utc)



@router.get("/summary")
async def dashboard_summary(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Pipeline / problem / task stats for the authenticated user."""
    # Pipelines (exclude removed)
    pipelines_result = await db.execute(
        select(Pipeline).where(Pipeline.user_id == uid, Pipeline.removed_at == None)
    )
    pipelines = list(pipelines_result.scalars().all())

    active = [p for p in pipelines if p.launched_at is None]
    launched = [p for p in pipelines if p.launched_at is not None]

    # Per-pipeline problem counts
    problem_counts_result = await db.execute(
        select(Problem.pipeline_id, func.count())
        .where(Problem.user_id == uid)
        .group_by(Problem.pipeline_id)
    )
    problem_counts = {row[0]: row[1] for row in problem_counts_result.all()}

    # Per-pipeline task counts (total + open)
    task_counts_result = await db.execute(
        select(
            Task.pipeline_id,
            func.count(),
            func.count().filter(Task.status != "done"),
        )
        .where(Task.user_id == uid)
        .group_by(Task.pipeline_id)
    )
    total_task_counts: dict[str, int] = {}
    open_task_counts: dict[str, int] = {}
    for row in task_counts_result.all():
        total_task_counts[row[0]] = row[1]
        open_task_counts[row[0]] = row[2]

    problems_total = sum(problem_counts.values())
    tasks_total = sum(total_task_counts.values())
    open_tasks_total = sum(open_task_counts.values())

    def _pipeline_row(p: Pipeline) -> dict:
        return {
            "id": p.id,
            "name": p.name,
            "stage": p.stage,
            "status": p.status,
            "sourceClusterId": p.source_cluster_id,
            "postCount": len(p.post_ids or []),
            "problemCount": problem_counts.get(p.id, 0),
            "taskCount": total_task_counts.get(p.id, 0),
            "openTaskCount": open_task_counts.get(p.id, 0),
            "launchedAt": p.launched_at.isoformat() if p.launched_at else None,
            "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
        }

    recent = sorted(pipelines, key=lambda p: p.updated_at, reverse=True)[:5]

    return {
        "clustersTracked": len(pipelines),
        "activeCount": len(active),
        "launchedCount": len(launched),
        "problemsDefined": problems_total,
        "openTasks": open_tasks_total,
        "totalTasks": tasks_total,
        "pipelines": [_pipeline_row(p) for p in active],
        "recentActivity": [
            {
                "id": p.id,
                "name": p.name,
                "stage": p.stage,
                "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
            }
            for p in recent
        ],
    }


@router.get("/signals")
async def dashboard_signals(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Intelligence dashboard: top clusters by signal_score plus a domain
    breakdown computed live from cluster_items.opportunity_domain."""
    try:
        cluster_rows = list((await db.execute(
            select(Cluster, ClusterSignal)
            .outerjoin(ClusterSignal, ClusterSignal.cluster_id == Cluster.id)
            .where(Cluster.pipeline_version == PIPELINE_VERSION)
            .order_by(func.coalesce(ClusterSignal.signal_score, Cluster.signal_score).desc().nullslast())
            .limit(limit)
        )).all())
    except SQLAlchemyError:
        await db.rollback()
        cluster_rows = [
            (cluster, None)
            for cluster in (await db.execute(
                select(Cluster)
                .where(Cluster.pipeline_version == PIPELINE_VERSION)
                .order_by(Cluster.signal_score.desc().nullslast())
                .limit(limit)
            )).scalars().all()
        ]
    clusters = [cluster for cluster, _signal in cluster_rows]

    cluster_ids = [c.id for c in clusters]
    post_counts: dict[int, int] = {}
    if cluster_ids:
        rows = await db.execute(
            select(ClusterItem.cluster_id, func.count())
            .where(
                ClusterItem.cluster_id.in_(cluster_ids),
                ClusterItem.pipeline_version == PIPELINE_VERSION,
            )
            .group_by(ClusterItem.cluster_id)
        )
        post_counts = {cid: count for cid, count in rows.all()}

    # Domain breakdown across the current pipeline version.
    domain_rows = await db.execute(
        select(
            ClusterItem.opportunity_domain,
            func.count().label("post_count"),
            func.count(func.distinct(ClusterItem.cluster_id)).label("cluster_count"),
        )
        .where(
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            ClusterItem.opportunity_domain.isnot(None),
            func.trim(ClusterItem.opportunity_domain) != "",
        )
        .group_by(ClusterItem.opportunity_domain)
        .order_by(func.count().desc())
    )

    return {
        "clusters": [
            {
                "id": c.id,
                "name": c.name,
                "summary": c.summary,
                "signalScore": signal.signal_score if signal and signal.signal_score is not None else c.signal_score,
                "trending": c.trending,
                "postCount": post_counts.get(c.id, 0),
            }
            for c, signal in cluster_rows
        ],
        "domainBreakdown": [
            {"domain": domain, "postCount": post_count, "clusterCount": cluster_count}
            for domain, post_count, cluster_count in domain_rows.all()
        ],
    }


@router.get("/movers")
async def dashboard_movers(
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Momentum movers — the "what needs my attention" module. Top risers and
    fallers by 30-day momentum from ``cluster_signals``. Global (not project-
    scoped), matching the rest of the intelligence feed. Returns empty lists if
    ``cluster_signals`` is unavailable rather than failing the dashboard."""
    try:
        rows = list((await db.execute(
            select(Cluster, ClusterSignal)
            .join(ClusterSignal, ClusterSignal.cluster_id == Cluster.id)
            .where(
                Cluster.pipeline_version == PIPELINE_VERSION,
                ClusterSignal.momentum_30d.isnot(None),
            )
        )).all())
    except SQLAlchemyError:
        await db.rollback()
        return {"risers": [], "fallers": [], "available": False}

    def _row(cluster: Cluster, signal: ClusterSignal) -> dict:
        return {
            "id": cluster.id,
            "name": cluster.name,
            "momentum30d": signal.momentum_30d,
            "momentum7d": signal.momentum_7d,
            "signalScore": signal.signal_score if signal.signal_score is not None else cluster.signal_score,
            "postCount": signal.total_posts or 0,
            "trending": cluster.trending,
        }

    ranked = sorted(rows, key=lambda r: r[1].momentum_30d, reverse=True)
    risers = [_row(c, s) for c, s in ranked if s.momentum_30d > 0][:limit]
    # Fallers: most negative momentum first.
    fallers = [_row(c, s) for c, s in reversed(ranked) if s.momentum_30d < 0][:limit]
    return {"risers": risers, "fallers": fallers, "available": True}


@router.get("/activity")
async def dashboard_activity(
    weeks: int = Query(26, ge=4, le=53),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Discovery-activity heatmap + pipeline-health stats.

    There is no ``pipeline_runs`` table, so daily discovery volume is derived
    from ``cluster_items`` post timestamps (``posted_at``, falling back to
    ``scraped_at``) — the closest real signal of when discovery happened. Also
    returns last-activity / clusters-refreshed times and a 7-day-vs-prior trend
    for the data-health module.
    """
    now = _now()
    since = now - timedelta(weeks=weeks)
    activity_at = func.coalesce(ClusterItem.posted_at, ClusterItem.scraped_at)

    daily_rows = (await db.execute(
        select(func.date(activity_at), func.count())
        .where(
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            activity_at.isnot(None),
            activity_at >= since,
        )
        .group_by(func.date(activity_at))
        .order_by(func.date(activity_at).asc())
    )).all()
    days = [
        {"date": d.isoformat() if hasattr(d, "isoformat") else str(d), "count": int(count)}
        for d, count in daily_rows
    ]

    by_date = {row["date"]: row["count"] for row in days}

    def _window_sum(start_offset: int, end_offset: int) -> int:
        total = 0
        for n in range(start_offset, end_offset):
            key = (now - timedelta(days=n)).date().isoformat()
            total += by_date.get(key, 0)
        return total

    current_7d = _window_sum(0, 7)
    previous_7d = _window_sum(7, 14)
    change_pct = round((current_7d - previous_7d) / previous_7d, 4) if previous_7d else None

    total_posts = int(await db.scalar(
        select(func.count()).select_from(ClusterItem).where(ClusterItem.pipeline_version == PIPELINE_VERSION)
    ) or 0)
    last_activity_at = await db.scalar(
        select(func.max(activity_at)).where(ClusterItem.pipeline_version == PIPELINE_VERSION)
    )

    clusters_refreshed_at = None
    try:
        clusters_refreshed_at = await db.scalar(select(func.max(ClusterSignal.generated_at)))
    except SQLAlchemyError:
        await db.rollback()

    return {
        "weeks": weeks,
        "days": days,
        "totalPosts": total_posts,
        "windowPosts": sum(by_date.values()),
        "lastActivityAt": last_activity_at.isoformat() if last_activity_at else None,
        "clustersRefreshedAt": clusters_refreshed_at.isoformat() if clusters_refreshed_at else None,
        "trend": {
            "current7d": current_7d,
            "previous7d": previous_7d,
            "changePct": change_pct,
        },
    }
