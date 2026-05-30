from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db import get_db
from app.auth import get_uid
from app.models import Task
from app.limits import get_plan, enforce_tasks_limit

router = APIRouter(prefix="/tasks", tags=["tasks"])


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
        "createdAt": t.created_at.isoformat() if t.created_at else None,
        "updatedAt": t.updated_at.isoformat() if t.updated_at else None,
    }


class CreateTaskBody(BaseModel):
    pipeline_id: str
    title: str
    description: Optional[str] = None
    problem_id: Optional[str] = None


class PatchTaskBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    position: Optional[int] = None
    problem_id: Optional[str] = None


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


@router.post("")
async def create_task(
    body: CreateTaskBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    # Free plan: cap tasks per pipeline (403 with limit_reached on overflow).
    plan = await get_plan(uid, db)
    await enforce_tasks_limit(uid, plan, body.pipeline_id, db)

    task = Task(
        pipeline_id=body.pipeline_id,
        user_id=uid,
        title=body.title,
        description=body.description,
        problem_id=body.problem_id,
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
