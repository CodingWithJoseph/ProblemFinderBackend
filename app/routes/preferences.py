"""User preferences + data export.

* ``GET/PUT /preferences`` — notification settings (alert emails on/off, digest
  cadence, optional alternate email) and workspace defaults.
* ``GET /preferences/export`` — the caller's own pipelines, problems, and tasks
  as JSON, for a self-serve data export.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.db import get_db
from app.models import Pipeline, Problem, Task, UserPreference
from app.services.preferences import VALID_CADENCES, get_preference_row

router = APIRouter(prefix="/preferences", tags=["preferences"])


class PreferencesBody(BaseModel):
    # All optional; only provided fields are applied. A null clears that field.
    alerts_email_enabled: bool | None = None
    digest_cadence: str | None = None
    alert_email: str | None = Field(default=None, max_length=256)
    default_pipeline_id: str | None = Field(default=None, max_length=64)
    default_landing: str | None = Field(default=None, max_length=64)


def _serialize(row: UserPreference | None) -> dict:
    return {
        "alertsEmailEnabled": True if row is None else (True if row.alerts_email_enabled is None else row.alerts_email_enabled),
        "digestCadence": "instant" if row is None else (row.digest_cadence or "instant"),
        "alertEmail": None if row is None else row.alert_email,
        "defaultPipelineId": None if row is None else row.default_pipeline_id,
        "defaultLanding": None if row is None else row.default_landing,
    }


@router.get("")
async def get_preferences(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    return {"data": _serialize(await get_preference_row(db, uid))}


@router.put("")
async def update_preferences(
    body: PreferencesBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    updates = body.model_dump(exclude_unset=True)

    if "digest_cadence" in updates and updates["digest_cadence"] not in VALID_CADENCES:
        raise HTTPException(status_code=400, detail=f"digest_cadence must be one of {list(VALID_CADENCES)}")
    if updates.get("alert_email"):
        if "@" not in updates["alert_email"]:
            raise HTTPException(status_code=400, detail="alert_email must be a valid email")

    row = await get_preference_row(db, uid)
    now = datetime.now(timezone.utc)
    if row is None:
        row = UserPreference(uid=uid, updated_at=now)
        db.add(row)
    for field, value in updates.items():
        setattr(row, field, value)
    row.updated_at = now
    await db.commit()
    await db.refresh(row)
    return {"data": _serialize(row)}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@router.get("/export")
async def export_my_data(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    pipelines = list((await db.execute(
        select(Pipeline).where(Pipeline.user_id == uid).order_by(Pipeline.created_at.asc())
    )).scalars().all())
    problems = list((await db.execute(
        select(Problem).where(Problem.user_id == uid).order_by(Problem.created_at.asc())
    )).scalars().all())
    tasks = list((await db.execute(
        select(Task).where(Task.user_id == uid).order_by(Task.created_at.asc())
    )).scalars().all())

    return {
        "data": {
            "exportedAt": datetime.now(timezone.utc).isoformat(),
            "pipelines": [
                {
                    "id": p.id, "name": p.name, "stage": p.stage, "status": p.status,
                    "url": p.url, "notes": p.notes, "mrr": p.mrr,
                    "launchedAt": _iso(p.launched_at), "createdAt": _iso(p.created_at),
                }
                for p in pipelines
            ],
            "problems": [
                {
                    "id": p.id, "pipelineId": p.pipeline_id, "title": p.title,
                    "description": p.description, "createdAt": _iso(p.created_at),
                }
                for p in problems
            ],
            "tasks": [
                {
                    "id": t.id, "pipelineId": t.pipeline_id, "problemId": t.problem_id,
                    "title": t.title, "description": t.description, "status": t.status,
                    "createdAt": _iso(t.created_at),
                }
                for t in tasks
            ],
        }
    }
