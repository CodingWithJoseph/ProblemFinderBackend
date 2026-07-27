"""Deterministic Signal case lifecycle helpers.

This module owns case/job creation and response assembly. Model execution lives
in a separate worker so Pipeline writes stay fast and opening a valid case never
causes an implicit regeneration.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Iterable, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ClusterItem,
    Pipeline,
    SignalAnalysisCase,
    SignalAnalysisJob,
    SignalAnalysisVersion,
)
from app.signal_contract import SignalAnalysisProgress, SignalCaseDocument


SignalJobKind = Literal["initial", "refresh"]
ACTIVE_JOB_STATUSES = ("queued", "running")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def signal_source_fingerprint(items: Iterable[ClusterItem]) -> str:
    """Return an order-independent fingerprint for material source evidence."""

    normalized = []
    for item in items:
        normalized.append({
            "id": str(item.id),
            "content_hash": item.content_hash,
            "title": item.title,
            "body": item.body,
            "problem_statement": item.problem_statement,
            "solution_angle": item.solution_angle,
            "score": item.score,
            "num_comments": item.num_comments,
            "posted_at": _iso(item.posted_at),
            "scraped_at": _iso(item.scraped_at),
        })
    normalized.sort(key=lambda row: row["id"])
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def signal_source_updated_at(items: Iterable[ClusterItem]) -> datetime | None:
    timestamps = [
        timestamp
        for item in items
        for timestamp in (item.scraped_at, item.posted_at)
        if timestamp is not None
    ]
    return max(timestamps) if timestamps else None


def build_empty_case_document(
    card: Pipeline,
    case: SignalAnalysisCase,
) -> SignalCaseDocument:
    progress = None
    if case.progress_step and case.progress_label:
        progress = SignalAnalysisProgress(
            step=case.progress_step,
            label=case.progress_label,
        )
    elif case.status in {"queued", "generating"}:
        progress = SignalAnalysisProgress(
            step="queued",
            label="Waiting to analyze this Signal",
        )

    return SignalCaseDocument(
        version=0,
        status=case.status,
        progress=progress,
        safe_error=case.safe_error,
        project={
            "pipeline_id": str(card.id),
            "project_name": card.project_name or card.name,
            "cluster_name": card.name if card.source_cluster_id else None,
            "source_fingerprint": case.source_fingerprint,
            "analyzed_at": _iso(case.analyzed_at),
            "source_updated_at": _iso(case.source_updated_at),
        },
        metrics={
            "signal_strength": None,
            "momentum_30d": None,
            "freshness_days": None,
            "evidence_count": len(card.post_ids or []),
            "author_count": None,
            "source_diversity": None,
        },
    )


def build_current_case_document(
    card: Pipeline,
    case: SignalAnalysisCase,
    version: SignalAnalysisVersion,
) -> SignalCaseDocument:
    """Validate a stored version, then overlay mutable lifecycle context."""

    document = SignalCaseDocument.model_validate(version.analysis)
    payload = document.model_dump(mode="json")
    payload["version"] = version.version
    payload["status"] = case.status
    payload["safe_error"] = case.safe_error
    payload["progress"] = (
        {
            "step": case.progress_step,
            "label": case.progress_label,
        }
        if case.progress_step and case.progress_label
        else None
    )
    payload["project"].update({
        "pipeline_id": str(card.id),
        "project_name": card.project_name or card.name,
        "cluster_name": card.name if card.source_cluster_id else None,
        "source_fingerprint": case.source_fingerprint or version.source_fingerprint,
        "analyzed_at": _iso(case.analyzed_at or version.generated_at),
        "source_updated_at": _iso(case.source_updated_at),
    })
    return SignalCaseDocument.model_validate(payload)


def new_signal_case_and_job(
    card: Pipeline,
    uid: str,
    *,
    kind: SignalJobKind = "initial",
    now: datetime | None = None,
) -> tuple[SignalAnalysisCase, SignalAnalysisJob]:
    """Build a new case and its first queued job without database round trips."""

    created_at = now or utc_now()
    case = SignalAnalysisCase(
        id=str(uuid.uuid4()),
        pipeline_id=str(card.id),
        user_id=uid,
        status="queued",
        progress_step="queued",
        progress_label="Waiting to analyze this Signal",
        created_at=created_at,
        updated_at=created_at,
    )
    job = SignalAnalysisJob(
        id=str(uuid.uuid4()),
        case_id=case.id,
        requested_by=uid,
        kind=kind,
        status="queued",
        attempt=0,
        max_attempts=2,
        created_at=created_at,
        updated_at=created_at,
    )
    return case, job


async def persist_new_signal_case(
    db: AsyncSession,
    card: Pipeline,
    uid: str,
    *,
    kind: SignalJobKind = "initial",
) -> SignalAnalysisCase:
    case, job = new_signal_case_and_job(card, uid, kind=kind)
    db.add(case)
    db.add(job)
    await db.commit()
    return case


async def find_signal_case(
    db: AsyncSession,
    pipeline_id: str,
    uid: str,
) -> SignalAnalysisCase | None:
    return (await db.execute(
        select(SignalAnalysisCase).where(
            SignalAnalysisCase.pipeline_id == pipeline_id,
            SignalAnalysisCase.user_id == uid,
        )
    )).scalar_one_or_none()


async def find_current_version(
    db: AsyncSession,
    case: SignalAnalysisCase,
) -> SignalAnalysisVersion | None:
    if not case.current_version_id:
        return None
    return (await db.execute(
        select(SignalAnalysisVersion).where(
            SignalAnalysisVersion.id == case.current_version_id,
            SignalAnalysisVersion.case_id == case.id,
        )
    )).scalar_one_or_none()


async def queue_signal_refresh(
    db: AsyncSession,
    case: SignalAnalysisCase,
    uid: str,
) -> SignalAnalysisJob:
    active = (await db.execute(
        select(SignalAnalysisJob).where(
            SignalAnalysisJob.case_id == case.id,
            SignalAnalysisJob.status.in_(ACTIVE_JOB_STATUSES),
        )
    )).scalar_one_or_none()
    if active:
        return active

    now = utc_now()
    job = SignalAnalysisJob(
        id=str(uuid.uuid4()),
        case_id=case.id,
        requested_by=uid,
        kind="refresh",
        status="queued",
        attempt=0,
        max_attempts=2,
        created_at=now,
        updated_at=now,
    )
    case.status = "queued"
    case.progress_step = "queued"
    case.progress_label = "Waiting to refresh this Signal"
    case.safe_error = None
    case.updated_at = now
    db.add(job)
    await db.commit()
    return job


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None

