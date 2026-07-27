"""Durable worker for versioned Signal analysis jobs."""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PIPELINE_VERSION, Settings, get_settings
from app.db import AsyncSessionLocal
from app.models import (
    ClusterItem,
    ClusterSignal,
    Pipeline,
    SignalAnalysisCase,
    SignalAnalysisJob,
    SignalAnalysisVersion,
)
from app.prompts.signal_analysis import SIGNAL_ANALYSIS_INSTRUCTIONS
from app.services.signal_analysis_provider import (
    SignalAnalysisProvider,
    SignalAnalysisProviderFailure,
    get_signal_analysis_provider,
)
from app.services.signal_cases import (
    signal_source_fingerprint,
    signal_source_updated_at,
    utc_now,
)
from app.signal_contract import (
    SIGNAL_ANALYSIS_SCHEMA_VERSION,
    SignalAnalysisModelOutput,
    SignalCaseDocument,
)


logger = logging.getLogger(__name__)
LEASE_SECONDS = 240
SAFE_FAILURES = {
    "timeout": "Signal analysis timed out. You can retry without losing the previous version.",
    "connection": "The Signal analysis provider is unavailable. You can retry later.",
    "missing_credentials": "Signal analysis is not configured for this environment.",
    "invalid_configuration": "Signal analysis is not configured for this environment.",
    "invalid_provider": "Signal analysis is not configured for this environment.",
    "refusal": "The Signal analysis provider could not process this evidence.",
    "empty_response": "Signal analysis returned no usable result. You can retry.",
    "invalid_json": "Signal analysis returned an invalid result. You can retry.",
    "schema_invalid": "Signal analysis returned an invalid result. You can retry.",
    "invalid_response": "Signal analysis returned an invalid result. You can retry.",
    "provider_error": "Signal analysis could not be completed. You can retry.",
    "validation": "Signal analysis could not verify all evidence citations. You can retry.",
    "missing_case": "The Signal project is no longer available.",
}


async def lease_next_signal_job(
    db: AsyncSession,
    worker_id: str,
    *,
    now: datetime | None = None,
) -> str | None:
    leased_at = now or utc_now()
    job = (await db.execute(
        select(SignalAnalysisJob)
        .where(
            SignalAnalysisJob.attempt < SignalAnalysisJob.max_attempts,
            or_(
                SignalAnalysisJob.status == "queued",
                (
                    (SignalAnalysisJob.status == "running")
                    & (SignalAnalysisJob.lease_expires_at < leased_at)
                ),
            ),
        )
        .order_by(SignalAnalysisJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )).scalar_one_or_none()
    if job is None:
        return None

    job.status = "running"
    job.attempt = int(job.attempt or 0) + 1
    job.lease_owner = worker_id
    job.lease_expires_at = leased_at + timedelta(seconds=LEASE_SECONDS)
    job.started_at = job.started_at or leased_at
    job.updated_at = leased_at

    case = (await db.execute(
        select(SignalAnalysisCase).where(SignalAnalysisCase.id == job.case_id)
    )).scalar_one_or_none()
    if case is not None:
        case.status = "generating"
        case.progress_step = "preparing_evidence"
        case.progress_label = "Preparing source evidence"
        case.safe_error = None
        case.updated_at = leased_at

    await db.commit()
    return str(job.id)


async def process_signal_job(
    job_id: str,
    *,
    provider: SignalAnalysisProvider | None = None,
    settings: Settings | None = None,
) -> None:
    resolved_settings = settings or get_settings()
    try:
        resolved_provider = provider or get_signal_analysis_provider(resolved_settings)
    except SignalAnalysisProviderFailure as exc:
        async with AsyncSessionLocal() as db:
            await fail_signal_job(db, job_id, exc.category)
        return

    async with AsyncSessionLocal() as db:
        await execute_signal_job(db, job_id, resolved_provider, resolved_settings)


async def execute_signal_job(
    db: AsyncSession,
    job_id: str,
    provider: SignalAnalysisProvider,
    settings: Settings,
) -> None:
    job = (await db.execute(
        select(SignalAnalysisJob).where(SignalAnalysisJob.id == job_id)
    )).scalar_one_or_none()
    if job is None or job.status != "running":
        return

    case = (await db.execute(
        select(SignalAnalysisCase).where(SignalAnalysisCase.id == job.case_id)
    )).scalar_one_or_none()
    if case is None:
        await fail_signal_job(db, job_id, "missing_case", job=job)
        return

    card = (await db.execute(
        select(Pipeline).where(
            Pipeline.id == case.pipeline_id,
            Pipeline.user_id == case.user_id,
            Pipeline.removed_at == None,
        )
    )).scalar_one_or_none()
    if card is None:
        await fail_signal_job(db, job_id, "missing_case", job=job, case=case)
        return

    all_items = await load_signal_source_items(db, card)
    selected = select_representative_evidence(
        all_items,
        max_items=max(1, int(settings.signal_analysis_max_evidence)),
    )
    fingerprint = signal_source_fingerprint(all_items)
    updated_at = signal_source_updated_at(all_items)
    job.source_fingerprint = fingerprint
    case.source_fingerprint = fingerprint
    case.source_updated_at = updated_at

    if len(selected) < max(1, int(settings.signal_analysis_min_evidence)):
        await complete_insufficient_evidence(db, job, case)
        return

    case.progress_step = "analyzing_problem"
    case.progress_label = "Analyzing the problem"
    case.updated_at = utc_now()
    await db.commit()

    cluster_signal = await load_cluster_signal(db, card)
    payload = build_analysis_payload(card, selected, cluster_signal)
    try:
        output = await provider.generate(
            instructions=SIGNAL_ANALYSIS_INSTRUCTIONS,
            payload=payload,
        )
        case.progress_step = "validating_citations"
        case.progress_label = "Validating evidence citations"
        await db.commit()

        document = build_case_document(
            card,
            case,
            all_items,
            selected,
            cluster_signal,
            output,
        )
        next_version = int(await db.scalar(
            select(func.coalesce(func.max(SignalAnalysisVersion.version), 0)).where(
                SignalAnalysisVersion.case_id == case.id
            )
        ) or 0) + 1
        await save_signal_version(
            db,
            job,
            case,
            document,
            next_version,
            provider.name,
            provider.model,
            fingerprint,
        )
    except SignalAnalysisProviderFailure as exc:
        await fail_signal_job(db, job_id, exc.category, job=job, case=case)
    except ValidationError:
        logger.warning("Signal analysis citation validation failed case=%s", case.id)
        await fail_signal_job(db, job_id, "validation", job=job, case=case)
    except Exception:
        logger.exception("Signal analysis failed case=%s", case.id)
        await fail_signal_job(db, job_id, "provider_error", job=job, case=case)


async def load_signal_source_items(db: AsyncSession, card: Pipeline) -> list[ClusterItem]:
    cluster_id = _cluster_id(card)
    statement = select(ClusterItem).where(
        ClusterItem.pipeline_version == PIPELINE_VERSION,
    )
    if cluster_id is not None:
        statement = statement.where(ClusterItem.cluster_id == cluster_id)
    else:
        statement = statement.where(ClusterItem.id.in_(card.post_ids or []))
    result = await db.execute(statement.order_by(ClusterItem.score.desc().nullslast()))
    return list(result.scalars().all())


async def load_cluster_signal(db: AsyncSession, card: Pipeline) -> ClusterSignal | None:
    cluster_id = _cluster_id(card)
    if cluster_id is None:
        return None
    return (await db.execute(
        select(ClusterSignal).where(ClusterSignal.cluster_id == cluster_id)
    )).scalar_one_or_none()


def select_representative_evidence(
    items: Iterable[ClusterItem],
    *,
    max_items: int,
) -> list[ClusterItem]:
    """Balance high engagement with recent evidence, deterministically."""

    usable = [
        item
        for item in items
        if (item.problem_statement or item.body or item.title)
    ]
    by_engagement = sorted(
        usable,
        key=lambda item: (
            -(item.score or 0),
            -(item.num_comments or 0),
            str(item.id),
        ),
    )
    by_recency = sorted(
        usable,
        key=lambda item: (
            -_timestamp(item.posted_at or item.scraped_at),
            str(item.id),
        ),
    )
    selected: list[ClusterItem] = []
    seen: set[str] = set()
    engagement_slots = (max_items + 1) // 2
    for pool, limit in (
        (by_engagement, engagement_slots),
        (by_recency, max_items - engagement_slots),
        (by_engagement, max_items),
    ):
        added = 0
        for item in pool:
            item_id = str(item.id)
            if item_id in seen:
                continue
            selected.append(item)
            seen.add(item_id)
            added += 1
            if len(selected) >= max_items or added >= limit:
                break
        if len(selected) >= max_items:
            break
    return selected


def build_analysis_payload(
    card: Pipeline,
    items: list[ClusterItem],
    signal: ClusterSignal | None,
) -> dict:
    return {
        "project": {
            "name": card.project_name or card.name,
            "clusterName": card.name if card.source_cluster_id else None,
            "stage": card.stage,
        },
        "metrics": {
            "signalStrength": signal.signal_score if signal else None,
            "momentum30d": signal.momentum_30d if signal else None,
            "totalPosts": signal.total_posts if signal else len(items),
            "authorCount": signal.author_count if signal else None,
            "sourceDiversity": signal.platform_count if signal else None,
        },
        "evidence": [
            {
                "id": str(item.id),
                "title": item.title or "Untitled evidence",
                "excerpt": _excerpt(item),
                "platform": item.platform,
                "community": item.community,
                "score": item.score,
                "commentCount": item.num_comments,
                "observedAt": _iso(item.posted_at or item.scraped_at),
                "problemStatement": item.problem_statement,
                "currentWorkaround": item.solution_angle,
            }
            for item in items
        ],
    }


def build_case_document(
    card: Pipeline,
    case: SignalAnalysisCase,
    all_items: list[ClusterItem],
    selected: list[ClusterItem],
    signal: ClusterSignal | None,
    output: SignalAnalysisModelOutput,
) -> SignalCaseDocument:
    claim_links: dict[str, list[str]] = {}
    for claim in output.claims:
        for evidence_id in claim.evidence_ids:
            claim_links.setdefault(evidence_id, []).append(claim.id)
    unit_links: dict[str, list[str]] = {}
    for unit in output.problem_units:
        for evidence_id in unit.evidence_ids:
            unit_links.setdefault(evidence_id, []).append(unit.id)

    evidence = []
    for item in selected:
        item_id = str(item.id)
        cited = bool(claim_links.get(item_id) or unit_links.get(item_id))
        evidence.append({
            "id": item_id,
            "title": item.title or "Untitled evidence",
            "excerpt": _excerpt(item),
            "body": None,
            "platform": item.platform,
            "community": item.community,
            "author": item.author,
            "observed_at": _iso(item.posted_at or item.scraped_at),
            "score": item.score,
            "comment_count": item.num_comments,
            "source_url": item.url or item.permalink,
            "stance": "supporting" if cited else "excluded",
            "claim_ids": claim_links.get(item_id, []),
            "problem_unit_ids": unit_links.get(item_id, []),
            "relevance_reason": (
                "Cited in the generated problem analysis."
                if cited
                else "Selected for review but not used by a generated claim."
            ),
            "pinned": False,
            "user_note": None,
        })

    latest = signal_source_updated_at(all_items)
    freshness_days = None
    if latest:
        freshness_days = max(0, (utc_now() - latest).days)
    authors = {item.author for item in all_items if item.author}
    platforms = {item.platform for item in all_items if item.platform}
    return SignalCaseDocument(
        version=1,
        status="ready",
        progress=None,
        safe_error=None,
        project={
            "pipeline_id": str(card.id),
            "project_name": card.project_name or card.name,
            "cluster_name": card.name if card.source_cluster_id else None,
            "source_fingerprint": case.source_fingerprint,
            "analyzed_at": utc_now().isoformat(),
            "source_updated_at": _iso(latest),
        },
        metrics={
            "signal_strength": signal.signal_score if signal else None,
            "momentum_30d": signal.momentum_30d if signal else None,
            "freshness_days": freshness_days,
            "evidence_count": len(all_items),
            "author_count": signal.author_count if signal else len(authors),
            "source_diversity": signal.platform_count if signal else len(platforms),
        },
        thesis=output.thesis,
        claims=output.claims,
        problem_units=output.problem_units,
        audiences=output.audiences,
        alternatives=output.alternatives,
        assumptions=output.assumptions,
        evidence=evidence,
        recommended_focus=output.recommended_focus,
    )


async def save_signal_version(
    db: AsyncSession,
    job: SignalAnalysisJob,
    case: SignalAnalysisCase,
    document: SignalCaseDocument,
    version_number: int,
    provider_name: str,
    model: str,
    fingerprint: str,
) -> SignalAnalysisVersion:
    now = utc_now()
    payload = document.model_copy(update={"version": version_number}).model_dump(
        mode="json",
        by_alias=True,
    )
    version = SignalAnalysisVersion(
        id=str(uuid.uuid4()),
        case_id=case.id,
        version=version_number,
        schema_version=SIGNAL_ANALYSIS_SCHEMA_VERSION,
        provider=provider_name,
        model=model,
        source_fingerprint=fingerprint,
        analysis=payload,
        generated_at=now,
    )
    db.add(version)
    case.current_version_id = version.id
    case.status = "ready"
    case.progress_step = None
    case.progress_label = None
    case.safe_error = None
    case.analyzed_at = now
    case.updated_at = now
    job.status = "succeeded"
    job.finished_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    job.error_category = None
    job.safe_error = None
    job.updated_at = now
    await db.commit()
    return version


async def complete_insufficient_evidence(
    db: AsyncSession,
    job: SignalAnalysisJob,
    case: SignalAnalysisCase,
) -> None:
    now = utc_now()
    case.status = "insufficient_evidence"
    case.progress_step = None
    case.progress_label = None
    case.safe_error = None
    case.analyzed_at = now
    case.updated_at = now
    job.status = "succeeded"
    job.finished_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = now
    await db.commit()


async def fail_signal_job(
    db: AsyncSession,
    job_id: str,
    category: str,
    *,
    job: SignalAnalysisJob | None = None,
    case: SignalAnalysisCase | None = None,
) -> None:
    failed_job = job or (await db.execute(
        select(SignalAnalysisJob).where(SignalAnalysisJob.id == job_id)
    )).scalar_one_or_none()
    if failed_job is None:
        return
    failed_case = case or (await db.execute(
        select(SignalAnalysisCase).where(SignalAnalysisCase.id == failed_job.case_id)
    )).scalar_one_or_none()
    now = utc_now()
    safe_error = SAFE_FAILURES.get(category, SAFE_FAILURES["provider_error"])
    failed_job.status = "failed"
    failed_job.error_category = category
    failed_job.safe_error = safe_error
    failed_job.finished_at = now
    failed_job.lease_owner = None
    failed_job.lease_expires_at = None
    failed_job.updated_at = now
    if failed_case is not None:
        failed_case.status = "failed"
        failed_case.progress_step = None
        failed_case.progress_label = None
        failed_case.safe_error = safe_error
        failed_case.updated_at = now
    await db.commit()


async def run_signal_worker(
    stop_event: asyncio.Event,
    *,
    settings: Settings | None = None,
    worker_id: str | None = None,
) -> None:
    resolved = settings or get_settings()
    identity = worker_id or f"{socket.gethostname()}:{uuid.uuid4()}"
    poll_seconds = max(0.25, min(float(resolved.signal_analysis_poll_seconds), 30.0))
    while not stop_event.is_set():
        job_id = None
        try:
            async with AsyncSessionLocal() as db:
                job_id = await lease_next_signal_job(db, identity)
            if job_id:
                await process_signal_job(job_id, settings=resolved)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Signal analysis worker iteration failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass


def _cluster_id(card: Pipeline) -> int | None:
    try:
        return int(card.source_cluster_id) if card.source_cluster_id else None
    except (TypeError, ValueError):
        return None


def _excerpt(item: ClusterItem) -> str:
    text = item.problem_statement or item.body or item.title or "No source excerpt available."
    compact = " ".join(text.split())
    return compact[:700]


def _timestamp(value: datetime | None) -> float:
    return value.timestamp() if value else 0.0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None

