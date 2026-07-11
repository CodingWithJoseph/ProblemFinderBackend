"""Beacon ingestion routes (the monitoring write-path).

These are the public, unauthenticated endpoints the browser/SDK beacon posts to
(/events, /identify, /errors, /vitals, /logs, /batch). Split out of monitor.py so
that file is just the authenticated dashboard read-path. All logic lives in
app.services.monitoring; these handlers only validate + dispatch.
"""

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.db import get_db
from app.models import (
    MonitorErrorEvent,
    MonitorIdentity,
    MonitorUsageEvent,
    MonitorUsageSource,
)
from app.services.monitoring.common import _clean_text, _now
from app.services.monitoring.ingest import (
    _build_usage_event,
    _check_rate_limit,
    _record_error_event,
    _record_log,
    _record_span,
    _record_web_vital,
)
from app.services.monitoring.sources import (
    _customer_email_index,
    _origin_allowed,
    _resolve_identity,
    _revenue_source,
    _usage_event_domain_allowed,
)

public_router = APIRouter(tags=["public-monitor"])

UsageEventType = Literal["pageview", "signup", "login", "activation", "custom"]
ErrorLevel = Literal["error", "warning"]
WebVitalMetric = Literal["LCP", "CLS", "INP", "FCP", "TTFB"]
WebVitalRating = Literal["good", "needs-improvement", "poor"]
LogLevel = Literal["debug", "info", "warn", "error"]


class UsageEventBody(BaseModel):
    product_id: str
    key: str
    event_type: UsageEventType
    visitor_id: str | None = None
    session_id: str | None = None
    user_id: str | None = Field(default=None, max_length=256)
    url: str | None = None
    referrer: str | None = None
    release: str | None = Field(default=None, max_length=128)
    environment: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)
    span_id: str | None = Field(default=None, max_length=64)
    parent_span_id: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=20)
    capture_mode: str | None = Field(default=None, max_length=10)
    metadata: dict | None = None
    occurred_at: datetime | None = None


class ErrorEventBody(BaseModel):
    product_id: str
    key: str
    message: str = Field(min_length=1, max_length=2000)
    stack: str | None = Field(default=None, max_length=20000)
    level: ErrorLevel = "error"
    handled: bool | None = None
    error_type: str | None = Field(default=None, max_length=40)
    url: str | None = None
    release: str | None = Field(default=None, max_length=128)
    environment: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)
    span_id: str | None = Field(default=None, max_length=64)
    parent_span_id: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=20)
    capture_mode: str | None = Field(default=None, max_length=10)
    visitor_id: str | None = None
    session_id: str | None = None
    user_id: str | None = Field(default=None, max_length=256)
    metadata: dict | None = None
    occurred_at: datetime | None = None


class IdentifyBody(BaseModel):
    product_id: str
    key: str
    user_id: str = Field(min_length=1, max_length=256)
    # Optional join traits. stripe_customer_id gives a direct join; email is the
    # fallback. group_id associates the actor with a company/account.
    stripe_customer_id: str | None = Field(default=None, max_length=256)
    email: str | None = Field(default=None, max_length=320)
    group_id: str | None = Field(default=None, max_length=256)
    traits: dict | None = None
    # Session context for backfill: when the beacon calls identify() mid-session,
    # these let the server attribute the session's prior anonymous events to the
    # now-known user_ref. Optional so older snippets keep working.
    session_id: str | None = None
    visitor_id: str | None = None


class VitalBody(BaseModel):
    product_id: str
    key: str
    metric: WebVitalMetric
    value: float
    rating: WebVitalRating | None = None
    url: str | None = None
    navigation_id: str | None = Field(default=None, max_length=128)
    visitor_id: str | None = None
    session_id: str | None = None
    user_id: str | None = Field(default=None, max_length=256)
    release: str | None = Field(default=None, max_length=128)
    environment: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)
    span_id: str | None = Field(default=None, max_length=64)
    parent_span_id: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=20)
    occurred_at: datetime | None = None


class LogBody(BaseModel):
    product_id: str
    key: str
    level: LogLevel
    message: str = Field(min_length=1, max_length=4000)
    metadata: dict | None = None
    url: str | None = None
    visitor_id: str | None = None
    session_id: str | None = None
    user_id: str | None = Field(default=None, max_length=256)
    release: str | None = Field(default=None, max_length=128)
    environment: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)
    span_id: str | None = Field(default=None, max_length=64)
    parent_span_id: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=20)
    occurred_at: datetime | None = None


class BatchItem(BaseModel):
    """One event in a batch. `kind` discriminates a usage event from an error, a
    web vital, or a log so each storage path (and error grouping) stays identical
    to the single endpoints."""
    kind: Literal["event", "error", "vital", "log", "span"]
    # usage-event field
    event_type: UsageEventType | None = None
    referrer: str | None = None
    # span fields (kind == "span"). span_kind is the span's own client/server/
    # internal role, distinct from the batch discriminator above.
    name: str | None = Field(default=None, max_length=200)
    span_kind: str | None = Field(default=None, max_length=20)
    service: str | None = Field(default=None, max_length=60)
    feature: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, max_length=20)
    duration_ms: float | None = None
    # error fields
    message: str | None = Field(default=None, max_length=4000)
    stack: str | None = Field(default=None, max_length=20000)
    level: ErrorLevel = "error"
    handled: bool | None = None
    # vital fields
    metric: WebVitalMetric | None = None
    value: float | None = None
    rating: WebVitalRating | None = None
    navigation_id: str | None = Field(default=None, max_length=128)
    # log field
    log_level: LogLevel | None = None
    # error dimension
    error_type: str | None = Field(default=None, max_length=40)
    # shared envelope
    url: str | None = None
    release: str | None = Field(default=None, max_length=128)
    environment: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)
    span_id: str | None = Field(default=None, max_length=64)
    parent_span_id: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=20)
    capture_mode: str | None = Field(default=None, max_length=10)
    visitor_id: str | None = None
    session_id: str | None = None
    user_id: str | None = Field(default=None, max_length=256)
    metadata: dict | None = None
    occurred_at: datetime | None = None


class BatchBody(BaseModel):
    product_id: str
    key: str
    batch: list[BatchItem] = Field(default_factory=list, max_length=100)


@public_router.post("/events")
async def ingest_usage_event(
    request: Request,
    body: UsageEventBody,
    db: AsyncSession = Depends(get_db),
):
    source = (await db.execute(
        select(MonitorUsageSource).where(
            MonitorUsageSource.pipeline_id == body.product_id,
            MonitorUsageSource.public_key == body.key,
            MonitorUsageSource.status == "connected",
        )
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Usage source not found")
    if not _usage_event_domain_allowed(source, request, body):
        raise HTTPException(status_code=403, detail="Usage event origin is not allowed")
    _check_rate_limit(source.id)

    now = _now()
    event = _build_usage_event(
        source,
        event_type=body.event_type,
        visitor_id=body.visitor_id,
        session_id=body.session_id,
        user_ref=body.user_id,
        url=body.url,
        referrer=body.referrer,
        release=body.release,
        environment=body.environment,
        trace_id=body.trace_id,
        span_id=body.span_id,
        parent_span_id=body.parent_span_id,
        platform=body.platform,
        capture_mode=body.capture_mode,
        metadata=body.metadata,
        occurred_at=body.occurred_at,
        now=now,
    )
    source.last_seen_at = now
    source.updated_at = now
    db.add(event)
    await db.commit()
    return {"success": True}


@public_router.post("/identify")
async def ingest_identify(
    request: Request,
    body: IdentifyBody,
    db: AsyncSession = Depends(get_db),
):
    """Associate a usage actor (user_ref) with a Stripe customer. Server-side so
    ad-blockers can't drop it. Upserts the identity and resolves it against the
    customer directory: explicit stripe_customer_id trait → direct join; else
    email match; else unresolved (resolved later when the directory has the
    customer's email)."""
    source = (await db.execute(
        select(MonitorUsageSource).where(
            MonitorUsageSource.pipeline_id == body.product_id,
            MonitorUsageSource.public_key == body.key,
            MonitorUsageSource.status == "connected",
        )
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Usage source not found")
    if not _origin_allowed(source.allowed_domain, request, None):
        raise HTTPException(status_code=403, detail="Identify origin is not allowed")

    now = _now()
    email = _clean_text(body.email)
    # Resolve against the revenue source's customer directory, if one exists.
    revenue = await _revenue_source(source.pipeline_id, db)
    email_index = await _customer_email_index(db, revenue.id) if revenue else {}
    customer_id, method = _resolve_identity(_clean_text(body.stripe_customer_id), email, email_index)

    existing = (await db.execute(
        select(MonitorIdentity).where(
            MonitorIdentity.pipeline_id == source.pipeline_id,
            MonitorIdentity.user_ref == body.user_id,
        )
    )).scalar_one_or_none()
    if existing is None:
        existing = MonitorIdentity(
            id=str(uuid.uuid4()),
            pipeline_id=source.pipeline_id,
            user_ref=body.user_id,
            created_at=now,
        )
        db.add(existing)
    existing.email = email or existing.email
    existing.group_id = _clean_text(body.group_id) or existing.group_id
    existing.traits = body.traits or existing.traits
    # Never downgrade an explicit join to a weaker method on a later call.
    if existing.resolution_method != "explicit" or method == "explicit":
        existing.stripe_customer_id = customer_id or existing.stripe_customer_id
        existing.resolution_method = method
        existing.resolved_at = now if method != "unresolved" else existing.resolved_at
    existing.updated_at = now

    # Backfill the current session: events fire before the user is known, so the
    # session's earlier rows carry a null user_ref. Once identify() runs, stamp
    # them with the user_ref so the whole session attributes to one actor. Scoped
    # to this session's still-anonymous rows, so a re-identify can't reassign
    # events already attributed to a different user.
    backfilled = 0
    if body.session_id:
        for model in (MonitorUsageEvent, MonitorErrorEvent):
            result = await db.execute(
                update(model)
                .where(
                    model.pipeline_id == source.pipeline_id,
                    model.session_id == body.session_id,
                    model.user_ref.is_(None),
                )
                .values(user_ref=body.user_id)
            )
            backfilled += result.rowcount or 0

    await db.commit()
    return {"success": True, "resolution": existing.resolution_method, "backfilled": backfilled}


@public_router.post("/errors")
async def ingest_error_event(
    request: Request,
    body: ErrorEventBody,
    db: AsyncSession = Depends(get_db),
):
    source = (await db.execute(
        select(MonitorUsageSource).where(
            MonitorUsageSource.pipeline_id == body.product_id,
            MonitorUsageSource.public_key == body.key,
            MonitorUsageSource.status == "connected",
        )
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Usage source not found")
    if not _origin_allowed(source.allowed_domain, request, body.url):
        raise HTTPException(status_code=403, detail="Error event origin is not allowed")
    _check_rate_limit(source.id)

    now = _now()
    event, group = await _record_error_event(
        source, db,
        message=body.message,
        stack=body.stack,
        level=body.level,
        handled=body.handled,
        error_type=body.error_type,
        url=body.url,
        release=body.release,
        environment=body.environment,
        trace_id=body.trace_id,
        span_id=body.span_id,
        parent_span_id=body.parent_span_id,
        platform=body.platform,
        capture_mode=body.capture_mode,
        visitor_id=body.visitor_id,
        session_id=body.session_id,
        user_ref=body.user_id,
        metadata=body.metadata,
        occurred_at=body.occurred_at,
        now=now,
    )
    source.last_seen_at = now
    source.updated_at = now
    await db.commit()
    return {"success": True, "groupId": group.id, "fingerprint": event.fingerprint}


@public_router.post("/vitals")
async def ingest_vital(
    request: Request,
    body: VitalBody,
    db: AsyncSession = Depends(get_db),
):
    source = (await db.execute(
        select(MonitorUsageSource).where(
            MonitorUsageSource.pipeline_id == body.product_id,
            MonitorUsageSource.public_key == body.key,
            MonitorUsageSource.status == "connected",
        )
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Usage source not found")
    if not _origin_allowed(source.allowed_domain, request, body.url):
        raise HTTPException(status_code=403, detail="Vital origin is not allowed")
    _check_rate_limit(source.id)

    now = _now()
    db.add(_record_web_vital(
        source,
        metric=body.metric,
        value=body.value,
        rating=body.rating,
        url=body.url,
        navigation_id=body.navigation_id,
        visitor_id=body.visitor_id,
        session_id=body.session_id,
        user_ref=body.user_id,
        release=body.release,
        environment=body.environment,
        trace_id=body.trace_id,
        span_id=body.span_id,
        parent_span_id=body.parent_span_id,
        platform=body.platform,
        occurred_at=body.occurred_at,
        now=now,
    ))
    source.last_seen_at = now
    source.updated_at = now
    await db.commit()
    return {"success": True}


@public_router.post("/logs")
async def ingest_log(
    request: Request,
    body: LogBody,
    db: AsyncSession = Depends(get_db),
):
    source = (await db.execute(
        select(MonitorUsageSource).where(
            MonitorUsageSource.pipeline_id == body.product_id,
            MonitorUsageSource.public_key == body.key,
            MonitorUsageSource.status == "connected",
        )
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Usage source not found")
    if not _origin_allowed(source.allowed_domain, request, body.url):
        raise HTTPException(status_code=403, detail="Log origin is not allowed")
    _check_rate_limit(source.id)

    now = _now()
    db.add(_record_log(
        source,
        level=body.level,
        message=body.message,
        metadata=body.metadata,
        url=body.url,
        visitor_id=body.visitor_id,
        session_id=body.session_id,
        user_ref=body.user_id,
        release=body.release,
        environment=body.environment,
        trace_id=body.trace_id,
        span_id=body.span_id,
        parent_span_id=body.parent_span_id,
        platform=body.platform,
        occurred_at=body.occurred_at,
        now=now,
    ))
    source.last_seen_at = now
    source.updated_at = now
    await db.commit()
    return {"success": True}


@public_router.post("/batch")
async def ingest_batch(
    request: Request,
    body: BatchBody,
    db: AsyncSession = Depends(get_db),
):
    """Store many events in one request. The beacon's delivery layer batches
    queued events here and flushes the queue in a single `sendBeacon` on page
    unload. Each item routes to the same storage path as its single endpoint."""
    source = (await db.execute(
        select(MonitorUsageSource).where(
            MonitorUsageSource.pipeline_id == body.product_id,
            MonitorUsageSource.public_key == body.key,
            MonitorUsageSource.status == "connected",
        )
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Usage source not found")
    # One origin check for the whole batch (events from a page share an origin),
    # via the Origin/Referer headers rather than each item's url.
    if not _origin_allowed(source.allowed_domain, request, None):
        raise HTTPException(status_code=403, detail="Batch origin is not allowed")
    # A batch costs its event total, so batching can't bypass the per-source limit.
    _check_rate_limit(source.id, cost=max(1, len(body.batch)))

    now = _now()
    stored = 0
    for item in body.batch:
        if item.kind == "event":
            if not item.event_type:
                continue
            db.add(_build_usage_event(
                source,
                event_type=item.event_type,
                visitor_id=item.visitor_id,
                session_id=item.session_id,
                user_ref=item.user_id,
                url=item.url,
                referrer=item.referrer,
                release=item.release,
                environment=item.environment,
                trace_id=item.trace_id,
                span_id=item.span_id,
                parent_span_id=item.parent_span_id,
                platform=item.platform,
                capture_mode=item.capture_mode,
                metadata=item.metadata,
                occurred_at=item.occurred_at,
                now=now,
            ))
            stored += 1
        elif item.kind == "error":
            if not item.message:
                continue
            await _record_error_event(
                source, db,
                message=item.message,
                stack=item.stack,
                level=item.level,
                handled=item.handled,
                error_type=item.error_type,
                url=item.url,
                release=item.release,
                environment=item.environment,
                trace_id=item.trace_id,
                span_id=item.span_id,
                parent_span_id=item.parent_span_id,
                platform=item.platform,
                capture_mode=item.capture_mode,
                visitor_id=item.visitor_id,
                session_id=item.session_id,
                user_ref=item.user_id,
                metadata=item.metadata,
                occurred_at=item.occurred_at,
                now=now,
            )
            stored += 1
        elif item.kind == "vital":
            if not item.metric or item.value is None:
                continue
            db.add(_record_web_vital(
                source,
                metric=item.metric,
                value=item.value,
                rating=item.rating,
                url=item.url,
                navigation_id=item.navigation_id,
                visitor_id=item.visitor_id,
                session_id=item.session_id,
                user_ref=item.user_id,
                release=item.release,
                environment=item.environment,
                trace_id=item.trace_id,
                span_id=item.span_id,
                parent_span_id=item.parent_span_id,
                platform=item.platform,
                occurred_at=item.occurred_at,
                now=now,
            ))
            stored += 1
        elif item.kind == "log":
            if not item.log_level or not item.message:
                continue
            db.add(_record_log(
                source,
                level=item.log_level,
                message=item.message,
                metadata=item.metadata,
                url=item.url,
                visitor_id=item.visitor_id,
                session_id=item.session_id,
                user_ref=item.user_id,
                release=item.release,
                environment=item.environment,
                trace_id=item.trace_id,
                span_id=item.span_id,
                parent_span_id=item.parent_span_id,
                platform=item.platform,
                occurred_at=item.occurred_at,
                now=now,
            ))
            stored += 1
        elif item.kind == "span":
            if not item.trace_id or not item.span_id or not item.name:
                continue
            db.add(_record_span(
                source,
                trace_id=item.trace_id,
                span_id=item.span_id,
                parent_span_id=item.parent_span_id,
                name=item.name,
                kind=item.span_kind,
                service=item.service,
                feature=item.feature,
                platform=item.platform,
                capture_mode=item.capture_mode,
                status=item.status,
                release=item.release,
                environment=item.environment,
                visitor_id=item.visitor_id,
                session_id=item.session_id,
                user_ref=item.user_id,
                attributes=item.metadata,
                start_at=item.occurred_at,
                duration_ms=item.duration_ms,
                now=now,
            ))
            stored += 1

    if stored:
        source.last_seen_at = now
        source.updated_at = now
    await db.commit()
    return {"success": True, "stored": stored}
