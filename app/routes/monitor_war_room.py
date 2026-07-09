import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.db import get_db
from app.models import MonitorInvestigation, MonitorInvestigationEntry
from app.services.monitoring.common import _now
from app.services.monitoring.serializers import _serialize_entry, _serialize_investigation
from app.services.monitoring.sources import _require_launched_product

router = APIRouter(tags=["monitor"])

EvidenceKind = Literal["issue", "problem", "session", "trace", "feature", "release", "log", "link"]


def _default_investigation_title(kind: EvidenceKind, ref_id: str) -> str:
    label = {
        "issue": "issue",
        "problem": "detected problem",
        "session": "session",
        "trace": "trace",
        "feature": "feature flow",
        "release": "release",
        "log": "log",
        "link": "external evidence",
    }[kind]
    return f"Investigate {label}: {ref_id}"


class InvestigationFromEvidenceCreate(BaseModel):
    kind: EvidenceKind
    ref_id: str = Field(min_length=1, max_length=256)
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=4000)
    body: str | None = Field(default=None, max_length=8000)
    metadata: dict | None = None


@router.post("/{pipeline_id}/investigations/from-evidence")
async def create_investigation_from_evidence(
    pipeline_id: str,
    body: InvestigationFromEvidenceCreate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Open a war-room investigation directly from an observable evidence item."""
    await _require_launched_product(pipeline_id, db, uid)
    now = _now()
    inv = MonitorInvestigation(
        id=str(uuid.uuid4()), pipeline_id=pipeline_id,
        title=body.title or _default_investigation_title(body.kind, body.ref_id),
        summary=body.summary, status="open",
        created_at=now, updated_at=now,
    )
    entry = MonitorInvestigationEntry(
        id=str(uuid.uuid4()), investigation_id=inv.id,
        kind=body.kind, ref_id=body.ref_id, body=body.body,
        event_metadata=body.metadata or {}, created_at=now,
    )
    db.add(inv)
    db.add(entry)
    await db.commit()
    return {
        "data": {
            "investigation": _serialize_investigation(inv),
            "timeline": [_serialize_entry(entry)],
        }
    }
