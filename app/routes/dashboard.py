from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.auth import get_uid
from app.models import Pipeline, Problem, Task

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


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
