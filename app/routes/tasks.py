from datetime import datetime, date, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db import get_db
from app.auth import get_uid
from app.models import Task, Pipeline, Problem

router = APIRouter(prefix="/tasks", tags=["tasks"])

# A task is "due soon" when its due date is within this many days of today
# (today inclusive) and it isn't done. Past-due + not done is "overdue".
DUE_SOON_DAYS = 3


def _serialize(t: Task) -> dict:
    return {
        "id": t.id,
        "problemId": t.problem_id,
        "pipelineId": t.pipeline_id,
        "userId": t.user_id,
        "title": t.title,
        "description": t.description,
        "status": t.status,
        "position": t.position,
        "dueDate": t.due_date.isoformat() if t.due_date else None,
        "createdAt": t.created_at.isoformat() if t.created_at else None,
        "updatedAt": t.updated_at.isoformat() if t.updated_at else None,
    }


class CreateTaskBody(BaseModel):
    pipeline_id: str
    title: str
    description: Optional[str] = None
    problem_id: Optional[str] = None
    due_date: Optional[date] = None


class PatchTaskBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    position: Optional[int] = None
    problem_id: Optional[str] = None
    due_date: Optional[date] = None


@router.get("")
async def list_tasks(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Task)
        .where(Task.pipeline_id == pipeline_id, Task.user_id == uid)
        .order_by(Task.position.asc(), Task.created_at.asc())
    )
    tasks = result.scalars().all()
    return {"data": [_serialize(t) for t in tasks]}


@router.get("/deadline-summary")
async def task_deadline_summary(
    pipeline_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Roll-up of open task due dates for a project header: how many are overdue
    vs. due soon, plus the soonest open due date. Done tasks are ignored."""
    rows = (await db.execute(
        select(Task.due_date, Task.status).where(
            Task.pipeline_id == pipeline_id,
            Task.user_id == uid,
            Task.due_date.isnot(None),
        )
    )).all()

    today = datetime.now(timezone.utc).date()
    soon_cutoff = today + timedelta(days=DUE_SOON_DAYS)
    overdue = 0
    due_soon = 0
    next_due: date | None = None
    for due, status in rows:
        if status == "done":
            continue
        if next_due is None or due < next_due:
            next_due = due
        if due < today:
            overdue += 1
        elif due <= soon_cutoff:
            due_soon += 1

    return {
        "overdue": overdue,
        "dueSoon": due_soon,
        "nextDueDate": next_due.isoformat() if next_due else None,
    }


@router.post("")
async def create_task(
    body: CreateTaskBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    # Verify the caller owns the target pipeline (prevents attaching tasks to
    # another user's pipeline_id).
    owns = (await db.execute(
        select(Pipeline.id).where(
            Pipeline.id == body.pipeline_id, Pipeline.user_id == uid
        )
    )).scalar_one_or_none()
    if owns is None:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    # If a problem is linked, it must belong to the caller too.
    if body.problem_id is not None:
        owns_problem = (await db.execute(
            select(Problem.id).where(
                Problem.id == body.problem_id, Problem.user_id == uid
            )
        )).scalar_one_or_none()
        if owns_problem is None:
            raise HTTPException(status_code=404, detail="Problem not found")

    task = Task(
        pipeline_id=body.pipeline_id,
        user_id=uid,
        title=body.title,
        description=body.description,
        problem_id=body.problem_id,
        due_date=body.due_date,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return {"data": _serialize(task)}


@router.patch("/{task_id}")
async def patch_task(
    task_id: str,
    body: PatchTaskBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.user_id == uid)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(task, field, value)
    task.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(task)
    return {"data": _serialize(task)}


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Task).where(Task.id == task_id, Task.user_id == uid)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await db.delete(task)
    await db.commit()
    return {"success": True}
