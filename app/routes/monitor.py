import hashlib
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.config import get_settings
from app.db import get_db
from app.models import (
    MonitorAlertSettings,
    MonitorCustomer,
    MonitorErrorEvent,
    MonitorErrorGroup,
    MonitorIdentity,
    MonitorInvestigation,
    MonitorInvestigationEntry,
    MonitorLog,
    MonitorProblem,
    MonitorReport,
    MonitorRevenueSource,
    MonitorSpan,
    MonitorUsageEvent,
    MonitorUsageSource,
    MonitorWebVital,
    Pipeline,
)
from app.services.alerts import AlertPrefs, alert_settings_row
from app.services.app_settings import effective_config

router = APIRouter(prefix="/portfolio", tags=["monitor"])
public_router = APIRouter(prefix="/public/portfolio", tags=["public-monitor"])
settings = get_settings()

UsageEventType = Literal["pageview", "signup", "login", "activation", "custom"]
ErrorLevel = Literal["error", "warning"]
WebVitalMetric = Literal["LCP", "CLS", "INP", "FCP", "TTFB"]
WebVitalRating = Literal["good", "needs-improvement", "poor"]
LogLevel = Literal["debug", "info", "warn", "error"]


def _selected_mrr(source: MonitorRevenueSource | None, engine: str) -> tuple[int | None, int | None]:
    """Return current/previous MRR for the configured revenue engine."""
    if not source:
        return None, None
    if engine == "invoice":
        return source.invoice_mrr_cents, source.previous_invoice_mrr_cents
    return source.current_mrr_cents, source.previous_mrr_cents


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


class UsageSourceUpdateBody(BaseModel):
    product_url: str | None = Field(default=None, max_length=2048)
    allowed_domain: str | None = Field(default=None, max_length=255)


class AlertSettingsBody(BaseModel):
    # All optional: only fields present in the request are applied. A null
    # threshold clears the override and reverts to the global default.
    new_issue_enabled: bool | None = None
    error_spike_enabled: bool | None = None
    signups_drop_enabled: bool | None = None
    revenue_drop_enabled: bool | None = None
    error_spike_multiplier: float | None = Field(default=None, ge=1.0, le=100.0)
    signups_drop_pct: float | None = Field(default=None, ge=0.05, le=1.0)
    revenue_drop_pct: float | None = Field(default=None, ge=0.05, le=1.0)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _serialize_usage_source(source: MonitorUsageSource | None) -> dict | None:
    if not source:
        return None
    return {
        "id": source.id,
        "pipelineId": source.pipeline_id,
        "publicKey": source.public_key,
        "name": source.name,
        "status": source.status,
        "productUrl": source.product_url,
        "allowedDomain": source.allowed_domain,
        "createdAt": _iso(source.created_at),
        "updatedAt": _iso(source.updated_at),
        "lastSeenAt": _iso(source.last_seen_at),
    }


def _serialize_usage_event(event: MonitorUsageEvent) -> dict:
    return {
        "id": event.id,
        "pipelineId": event.pipeline_id,
        "sourceId": event.source_id,
        "eventType": event.event_type,
        "visitorId": event.visitor_id,
        "sessionId": event.session_id,
        "userId": event.user_ref,
        "url": event.url,
        "referrer": event.referrer,
        "release": event.release,
        "environment": event.environment,
        "metadata": event.event_metadata or {},
        "occurredAt": _iso(event.occurred_at),
        "receivedAt": _iso(event.received_at),
    }


def _serialize_error_group(group: MonitorErrorGroup, affected_sessions: int | None = None) -> dict:
    payload = {
        "id": group.id,
        "pipelineId": group.pipeline_id,
        "fingerprint": group.fingerprint,
        "title": group.title,
        "level": group.level,
        "status": group.status,
        "errorType": group.error_type,
        "eventCount": int(group.event_count or 0),
        "lastRelease": group.last_release,
        "firstSeenAt": _iso(group.first_seen_at),
        "lastSeenAt": _iso(group.last_seen_at),
    }
    if affected_sessions is not None:
        payload["affectedSessions"] = int(affected_sessions)
    return payload


def _serialize_error_event(event: MonitorErrorEvent) -> dict:
    return {
        "id": event.id,
        "pipelineId": event.pipeline_id,
        "groupId": event.group_id,
        "fingerprint": event.fingerprint,
        "message": event.message,
        "stack": event.stack,
        "level": event.level,
        "handled": event.handled,
        "errorType": event.error_type,
        "platform": event.platform,
        "url": event.url,
        "release": event.release,
        "environment": event.environment,
        "traceId": event.trace_id,
        "spanId": event.span_id,
        "visitorId": event.visitor_id,
        "sessionId": event.session_id,
        "userId": event.user_ref,
        "metadata": event.event_metadata or {},
        "occurredAt": _iso(event.occurred_at),
        "receivedAt": _iso(event.received_at),
    }


def _serialize_span(span: MonitorSpan, depth: int = 0) -> dict:
    return {
        "id": span.id,
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "parentSpanId": span.parent_span_id,
        "name": span.name,
        "kind": span.kind,
        "service": span.service,
        "feature": span.feature,
        "platform": span.platform,
        "status": span.status,
        "release": span.release,
        "environment": span.environment,
        "durationMs": span.duration_ms,
        "startAt": _iso(span.start_at),
        "attributes": span.attributes or {},
        "depth": depth,
    }


def _order_span_tree(spans: list) -> list[tuple]:
    """Preorder spans into a parent→child tree with depth. A span whose parent
    isn't in the trace (e.g. a server span whose client parent was sampled out)
    is treated as a root. Visited-guarded so a malformed parent cycle can't loop,
    and any span not reached from a root is appended so nothing is dropped."""
    ids = {s.span_id for s in spans}
    by_parent: dict = {}
    for s in spans:
        key = s.parent_span_id if s.parent_span_id in ids else None
        by_parent.setdefault(key, []).append(s)
    for children in by_parent.values():
        children.sort(key=lambda s: s.start_at or datetime.min.replace(tzinfo=timezone.utc))

    ordered: list[tuple] = []
    visited: set = set()

    def walk(parent_key, depth):
        for s in by_parent.get(parent_key, []):
            if s.span_id in visited:
                continue
            visited.add(s.span_id)
            ordered.append((s, depth))
            walk(s.span_id, depth + 1)

    walk(None, 0)
    for s in spans:
        if s.span_id not in visited:
            ordered.append((s, 0))
    return ordered


async def _revenue_source(pipeline_id: str, db: AsyncSession, provider: str = "stripe") -> MonitorRevenueSource | None:
    return (await db.execute(
        select(MonitorRevenueSource)
        .where(
            MonitorRevenueSource.pipeline_id == pipeline_id,
            MonitorRevenueSource.provider == provider,
        )
        .order_by(MonitorRevenueSource.created_at.asc())
    )).scalar_one_or_none()


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _domain_from_url(value: str | None) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    parsed = urlparse(cleaned if "://" in cleaned else f"https://{cleaned}")
    host = parsed.hostname or cleaned
    return host.lower().removeprefix("www.")


def _host_matches_allowed(host: str | None, allowed_domain: str | None) -> bool:
    host = _domain_from_url(host)
    allowed = _domain_from_url(allowed_domain)
    if not host or not allowed:
        return False
    return host == allowed or host.endswith(f".{allowed}")


def _origin_allowed(allowed_domain: str | None, request: Request, url: str | None) -> bool:
    if not allowed_domain:
        return True
    candidates = [
        request.headers.get("origin"),
        request.headers.get("referer"),
        url,
    ]
    return any(_host_matches_allowed(candidate, allowed_domain) for candidate in candidates)


def _usage_event_domain_allowed(source: MonitorUsageSource, request: Request, body: UsageEventBody) -> bool:
    return _origin_allowed(source.allowed_domain, request, body.url)


# Fingerprinting: collapse near-identical errors ("user 123 not found" /
# "user 456 not found") into one issue. We normalize volatile bits out of the
# message and pin to the top stack frame so distinct call sites stay distinct.
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_NUM_RE = re.compile(r"\d+")


def _normalize_error_message(message: str) -> str:
    text = (message or "").strip()
    text = _UUID_RE.sub("<uuid>", text)
    text = _HEX_RE.sub("<hex>", text)
    text = _NUM_RE.sub("<n>", text)
    return " ".join(text.split())


def _top_stack_frame(stack: str | None) -> str:
    if not stack:
        return ""
    for raw in stack.splitlines():
        line = raw.strip()
        if line.startswith("at ") or line.startswith("File ") or "@" in line:
            return _NUM_RE.sub("<n>", line)
    return ""


def _error_fingerprint(message: str, stack: str | None) -> str:
    basis = f"{_normalize_error_message(message)}|{_top_stack_frame(stack)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


async def _error_group(pipeline_id: str, fingerprint: str, db: AsyncSession) -> MonitorErrorGroup | None:
    return (await db.execute(
        select(MonitorErrorGroup).where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.fingerprint == fingerprint,
        )
    )).scalar_one_or_none()


def _source_is_syncable(source: MonitorRevenueSource | None) -> bool:
    """A source can be synced when it's a connected account with a provider id,
    or a first-party (own-key) source. First-party sources have no
    ``provider_account_id`` — they read the platform account directly."""
    if not source or source.status != "connected":
        return False
    if source.account_mode == "first_party":
        return True
    return bool(source.provider_account_id)


def _revenue_source_is_connected(source: MonitorRevenueSource | None, pipeline_id: str) -> bool:
    """Require a usable revenue integration scoped to the requested product."""
    return bool(
        source
        and str(source.pipeline_id) == str(pipeline_id)
        and _source_is_syncable(source)
    )


async def _require_launched_product(pipeline_id: str, db: AsyncSession, uid: str) -> Pipeline:
    product = (await db.execute(
        select(Pipeline).where(
            Pipeline.id == pipeline_id,
            Pipeline.user_id == uid,
            Pipeline.launched_at != None,
        )
    )).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Launched product not found")
    return product


async def _usage_source(pipeline_id: str, db: AsyncSession) -> MonitorUsageSource | None:
    return (await db.execute(
        select(MonitorUsageSource)
        .where(MonitorUsageSource.pipeline_id == pipeline_id)
        .order_by(MonitorUsageSource.created_at.asc())
    )).scalar_one_or_none()


def _usage_source_is_connected(source: MonitorUsageSource | None, pipeline_id: str) -> bool:
    """Only report a usable source belonging to the requested product as connected."""
    return bool(
        source
        and str(source.pipeline_id) == str(pipeline_id)
        and source.status == "connected"
        and source.public_key
    )


def _health_verdict(*, connected: bool, last_seen_at, total_events: int, eff, now) -> dict:
    """Health verdict engine v0: a per-source computed state, so the UI renders a
    badge instead of making the user read a timestamp and guess. Freshness-only
    for now (errors/vitals/revenue fold in at v2). States: no-data / healthy /
    warning / unhealthy."""
    if not connected or last_seen_at is None or total_events <= 0:
        return {"state": "no-data", "label": "No data", "reason": "No events received yet",
                "lastSeenAt": _iso(last_seen_at), "ageHours": None}

    seen = last_seen_at if last_seen_at.tzinfo else last_seen_at.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - seen).total_seconds() / 3600.0)
    if age_hours <= eff.health_warning_hours:
        state, label, reason = "healthy", "Healthy", "Receiving events"
    elif age_hours <= eff.health_unhealthy_hours:
        state, label, reason = "warning", "Quiet", f"No events for {int(age_hours)}h"
    else:
        state, label, reason = "unhealthy", "Silent", f"No events for {int(age_hours)}h"
    return {"state": state, "label": label, "reason": reason,
            "lastSeenAt": _iso(last_seen_at), "ageHours": round(age_hours, 1)}


def _health_verdict_v2(*, connected, last_seen_at, total_events, error_rate, lcp_rating, eff, now) -> dict:
    """Health verdict engine v2: freshness + error rate + vitals → a richer
    operational state. Precedence is severity-ordered: nothing to judge, then
    gone silent, then failing/noisy on live data, then merely stale, else live."""
    def verdict(state, label, reason, age_hours):
        return {"state": state, "label": label, "reason": reason,
                "lastSeenAt": _iso(last_seen_at), "ageHours": round(age_hours, 1) if age_hours is not None else None}

    if not connected or last_seen_at is None or total_events <= 0:
        return verdict("no-data", "No data", "No events received yet", None)

    seen = last_seen_at if last_seen_at.tzinfo else last_seen_at.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - seen).total_seconds() / 3600.0)

    if age_hours > eff.health_unhealthy_hours:
        return verdict("silent", "Silent", f"No events for {int(age_hours)}h", age_hours)

    rate_pct = f"{round(error_rate * 100)}%" if error_rate is not None else None
    if (error_rate is not None and error_rate >= eff.health_error_rate_failing):
        return verdict("failing", "Failing", f"Error rate {rate_pct}", age_hours)
    if lcp_rating == "poor":
        return verdict("failing", "Failing", "Poor experience (LCP)", age_hours)
    if (error_rate is not None and error_rate >= eff.health_error_rate_noisy):
        return verdict("noisy", "Noisy", f"Error rate {rate_pct}", age_hours)
    if lcp_rating == "needs-improvement":
        return verdict("noisy", "Noisy", "Experience needs improvement (LCP)", age_hours)
    if age_hours > eff.health_warning_hours:
        return verdict("stale", "Stale", f"No events for {int(age_hours)}h", age_hours)
    return verdict("live", "Live", "Healthy and current", age_hours)


async def _source_health(pipeline_id: str, source, eff, now, db) -> tuple[dict, dict]:
    """Gather the signals the v2 verdict needs (freshness, error rate, LCP p75)
    and compute it. Returns (verdict, signals)."""
    last_seen = source.last_seen_at if source else None
    since = now - timedelta(days=eff.analytics_usage_window_days)

    total_events = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(MonitorUsageEvent.pipeline_id == pipeline_id)
    ) or 0)
    errors = int(await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
    ) or 0)
    sessions = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.session_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.session_id != None,
        )
    ) or 0)
    lcp_p75 = await db.scalar(
        select(func.percentile_cont(0.75).within_group(MonitorWebVital.value.asc())).where(
            MonitorWebVital.pipeline_id == pipeline_id,
            MonitorWebVital.occurred_at >= since,
            MonitorWebVital.metric == "LCP",
        )
    )
    error_rate = round(errors / sessions, 4) if sessions else None
    lcp_rating = _rate_vital("LCP", lcp_p75) if lcp_p75 is not None else None

    verdict = _health_verdict_v2(
        connected=_usage_source_is_connected(source, pipeline_id),
        last_seen_at=last_seen,
        total_events=total_events,
        error_rate=error_rate,
        lcp_rating=lcp_rating,
        eff=eff,
        now=now,
    )
    signals = {
        "errors": errors,
        "sessions": sessions,
        "errorRate": error_rate,
        "lcpP75": _round_vital("LCP", lcp_p75),
        "lcpRating": lcp_rating,
    }
    return verdict, signals


@router.get("/{pipeline_id}/health")
async def get_source_health(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """The v2 health verdict for a source, with the signals behind it."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    now = _now()
    verdict, signals = await _source_health(pipeline_id, source, eff, now, db)
    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": eff.analytics_usage_window_days,
            "health": verdict,
            "signals": signals,
        }
    }


@router.get("/{pipeline_id}/command-center")
async def get_command_center(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """The at-a-glance Connect screen: the v2 health verdict, week-over-week
    trends (visitors / signups / errors / revenue), and the top open issues —
    one read that says 'is this product OK, and what needs attention'."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    now = _now()
    growth_window = eff.analytics_growth_window_days
    week_start = now - timedelta(days=growth_window)
    prev_week_start = now - timedelta(days=2 * growth_window)

    verdict, signals = await _source_health(pipeline_id, source, eff, now, db)
    revenue = await _revenue_source(pipeline_id, db)

    async def _count(model, start, end, *extra):
        return int(await db.scalar(
            select(func.count(model.id)).where(
                model.pipeline_id == pipeline_id,
                model.occurred_at >= start,
                model.occurred_at < end,
                *extra,
            )
        ) or 0)

    async def _distinct_visitors(start, end):
        return int(await db.scalar(
            select(func.count(func.distinct(MonitorUsageEvent.visitor_id))).where(
                MonitorUsageEvent.pipeline_id == pipeline_id,
                MonitorUsageEvent.occurred_at >= start,
                MonitorUsageEvent.occurred_at < end,
                MonitorUsageEvent.visitor_id != None,
            )
        ) or 0)

    visitors_cur = await _distinct_visitors(week_start, now)
    visitors_prev = await _distinct_visitors(prev_week_start, week_start)
    signups_cur = await _count(MonitorUsageEvent, week_start, now, MonitorUsageEvent.event_type == "signup")
    signups_prev = await _count(MonitorUsageEvent, prev_week_start, week_start, MonitorUsageEvent.event_type == "signup")
    errors_cur = await _count(MonitorErrorEvent, week_start, now)
    errors_prev = await _count(MonitorErrorEvent, prev_week_start, week_start)

    def trend(current, previous):
        return {"current": int(current), "previous": int(previous), "changePct": _pct_change(current, previous)}

    rev_cur = revenue.current_mrr_cents if revenue else None
    rev_prev = revenue.previous_mrr_cents if revenue else None

    top_groups = list((await db.execute(
        select(MonitorErrorGroup)
        .where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.status == "unresolved",
        )
        .order_by(MonitorErrorGroup.last_seen_at.desc())
        .limit(3)
    )).scalars().all())

    return {
        "data": {
            "windowDays": eff.analytics_usage_window_days,
            "growthWindowDays": growth_window,
            "health": verdict,
            "signals": signals,
            "trends": {
                "visitors": trend(visitors_cur, visitors_prev),
                "signups": trend(signups_cur, signups_prev),
                "errors": trend(errors_cur, errors_prev),
                "revenue": {
                    "current": rev_cur,
                    "previous": rev_prev,
                    "changePct": _pct_change(rev_cur, rev_prev) if (rev_cur is not None and rev_prev) else None,
                },
            },
            "revenueConnected": _revenue_source_is_connected(revenue, pipeline_id),
            "topIssues": [
                {
                    "id": group.id,
                    "title": group.title,
                    "level": group.level,
                    "status": group.status,
                    "eventCount": int(group.event_count or 0),
                    "lastRelease": group.last_release,
                    "lastSeenAt": _iso(group.last_seen_at),
                }
                for group in top_groups
            ],
        }
    }


def _serialize_problem(problem: MonitorProblem) -> dict:
    return {
        "id": problem.id,
        "kind": problem.kind,
        "title": problem.title,
        "detail": problem.detail,
        "severity": problem.severity,
        "status": problem.status,
        "metric": problem.metric,
        "baseline": problem.baseline,
        "observed": problem.observed,
        "detectedAt": _iso(problem.detected_at),
        "resolvedAt": _iso(problem.resolved_at),
    }


@router.get("/{pipeline_id}/problems")
async def get_problems(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Monitoring problems for a product — the durable records the evaluator
    writes when a condition fires (3.4), most recent first."""
    await _require_launched_product(pipeline_id, db, uid)
    problems = list((await db.execute(
        select(MonitorProblem)
        .where(MonitorProblem.pipeline_id == pipeline_id)
        .order_by(MonitorProblem.detected_at.desc())
        .limit(50)
    )).scalars().all())
    return {"data": {"problems": [_serialize_problem(p) for p in problems]}}


@router.get("/{pipeline_id}/problems/{problem_id}")
async def get_problem_impact(
    pipeline_id: str,
    problem_id: str,
    window_hours: int = Query(default=24, ge=1, le=168, alias="windowHours"),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Problem impact (3.5): deterministic before / during / after counts of the
    problem's metric, in equal windows around when it was detected. Frames the
    blast radius — was it a blip or a sustained regression?"""
    await _require_launched_product(pipeline_id, db, uid)
    problem = (await db.execute(
        select(MonitorProblem).where(
            MonitorProblem.id == problem_id,
            MonitorProblem.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")

    detected = problem.detected_at
    if detected.tzinfo is None:
        detected = detected.replace(tzinfo=timezone.utc)
    span = timedelta(hours=window_hours)

    impact = {"metric": problem.metric, "windowHours": window_hours, "before": None, "during": None, "after": None}
    model_extra = {
        "errors": (MonitorErrorEvent, ()),
        "signups": (MonitorUsageEvent, (MonitorUsageEvent.event_type == "signup",)),
    }.get(problem.metric or "")
    if model_extra:
        model, extra = model_extra

        async def _count(start, end):
            return int(await db.scalar(
                select(func.count(model.id)).where(
                    model.pipeline_id == pipeline_id,
                    model.occurred_at >= start,
                    model.occurred_at < end,
                    *extra,
                )
            ) or 0)

        impact["before"] = await _count(detected - 2 * span, detected - span)
        impact["during"] = await _count(detected - span, detected)
        impact["after"] = await _count(detected, detected + span)

    return {"data": {"problem": _serialize_problem(problem), "impact": impact}}


# ── Phase 4: Investigate & Report ────────────────────────────────────────────

class InvestigationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=4000)


class InvestigationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=4000)
    status: Literal["open", "resolved", "archived"] | None = None


class EntryCreate(BaseModel):
    kind: Literal["note", "issue", "problem", "session", "link"]
    ref_id: str | None = Field(default=None, max_length=256)
    body: str | None = Field(default=None, max_length=8000)
    metadata: dict | None = None


class ReportCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=100000)
    investigation_id: str | None = None


def _serialize_investigation(inv: MonitorInvestigation) -> dict:
    return {
        "id": inv.id,
        "title": inv.title,
        "summary": inv.summary,
        "status": inv.status,
        "createdAt": _iso(inv.created_at),
        "updatedAt": _iso(inv.updated_at),
    }


def _serialize_entry(entry: MonitorInvestigationEntry) -> dict:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "refId": entry.ref_id,
        "body": entry.body,
        "metadata": entry.event_metadata or {},
        "createdAt": _iso(entry.created_at),
    }


def _serialize_report(report: MonitorReport) -> dict:
    return {
        "id": report.id,
        "title": report.title,
        "body": report.body,
        "investigationId": report.investigation_id,
        "createdAt": _iso(report.created_at),
        "updatedAt": _iso(report.updated_at),
    }


async def _get_investigation(pipeline_id: str, investigation_id: str, db) -> MonitorInvestigation:
    inv = (await db.execute(
        select(MonitorInvestigation).where(
            MonitorInvestigation.id == investigation_id,
            MonitorInvestigation.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return inv


@router.post("/{pipeline_id}/investigations")
async def create_investigation(
    pipeline_id: str,
    body: InvestigationCreate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    now = _now()
    inv = MonitorInvestigation(
        id=str(uuid.uuid4()), pipeline_id=pipeline_id,
        title=body.title, summary=body.summary, status="open",
        created_at=now, updated_at=now,
    )
    db.add(inv)
    await db.commit()
    return {"data": _serialize_investigation(inv)}


@router.get("/{pipeline_id}/investigations")
async def list_investigations(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    rows = list((await db.execute(
        select(MonitorInvestigation)
        .where(MonitorInvestigation.pipeline_id == pipeline_id)
        .order_by(MonitorInvestigation.created_at.desc())
        .limit(100)
    )).scalars().all())
    return {"data": {"investigations": [_serialize_investigation(i) for i in rows]}}


@router.get("/{pipeline_id}/investigations/{investigation_id}")
async def get_investigation(
    pipeline_id: str,
    investigation_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """An investigation and its timeline — evidence + notes ordered by time."""
    await _require_launched_product(pipeline_id, db, uid)
    inv = await _get_investigation(pipeline_id, investigation_id, db)
    entries = list((await db.execute(
        select(MonitorInvestigationEntry)
        .where(MonitorInvestigationEntry.investigation_id == investigation_id)
        .order_by(MonitorInvestigationEntry.created_at.asc())
    )).scalars().all())
    return {
        "data": {
            "investigation": _serialize_investigation(inv),
            "timeline": [_serialize_entry(e) for e in entries],
        }
    }


@router.patch("/{pipeline_id}/investigations/{investigation_id}")
async def update_investigation(
    pipeline_id: str,
    investigation_id: str,
    body: InvestigationUpdate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    inv = await _get_investigation(pipeline_id, investigation_id, db)
    if body.title is not None:
        inv.title = body.title
    if body.summary is not None:
        inv.summary = body.summary
    if body.status is not None:
        inv.status = body.status
    inv.updated_at = _now()
    await db.commit()
    return {"data": _serialize_investigation(inv)}


@router.post("/{pipeline_id}/investigations/{investigation_id}/entries")
async def add_investigation_entry(
    pipeline_id: str,
    investigation_id: str,
    body: EntryCreate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Add a timeline entry: a note, or evidence linking an issue/problem/session."""
    await _require_launched_product(pipeline_id, db, uid)
    inv = await _get_investigation(pipeline_id, investigation_id, db)
    if body.kind == "note" and not (body.body or "").strip():
        raise HTTPException(status_code=422, detail="A note needs a body")
    if body.kind != "note" and not body.ref_id:
        raise HTTPException(status_code=422, detail="Evidence needs a ref_id")
    now = _now()
    entry = MonitorInvestigationEntry(
        id=str(uuid.uuid4()), investigation_id=investigation_id,
        kind=body.kind, ref_id=body.ref_id, body=body.body,
        event_metadata=body.metadata or {}, created_at=now,
    )
    db.add(entry)
    inv.updated_at = now
    await db.commit()
    return {"data": _serialize_entry(entry)}


@router.post("/{pipeline_id}/reports")
async def create_report(
    pipeline_id: str,
    body: ReportCreate,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    now = _now()
    report = MonitorReport(
        id=str(uuid.uuid4()), pipeline_id=pipeline_id,
        investigation_id=body.investigation_id, title=body.title,
        body=body.body or "", created_at=now, updated_at=now,
    )
    db.add(report)
    await db.commit()
    return {"data": _serialize_report(report)}


@router.get("/{pipeline_id}/reports")
async def list_reports(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    rows = list((await db.execute(
        select(MonitorReport)
        .where(MonitorReport.pipeline_id == pipeline_id)
        .order_by(MonitorReport.created_at.desc())
        .limit(100)
    )).scalars().all())
    return {"data": {"reports": [_serialize_report(r) for r in rows]}}


async def _get_report(pipeline_id: str, report_id: str, db) -> MonitorReport:
    report = (await db.execute(
        select(MonitorReport).where(
            MonitorReport.id == report_id,
            MonitorReport.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.get("/{pipeline_id}/reports/{report_id}")
async def get_report(
    pipeline_id: str,
    report_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    report = await _get_report(pipeline_id, report_id, db)
    return {"data": _serialize_report(report)}


@router.get("/{pipeline_id}/reports/{report_id}/export")
async def export_report(
    pipeline_id: str,
    report_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Exportable view: the report rendered as downloadable Markdown."""
    await _require_launched_product(pipeline_id, db, uid)
    report = await _get_report(pipeline_id, report_id, db)
    markdown = f"# {report.title}\n\n_Generated {_iso(report.updated_at)}_\n\n{report.body}\n"
    return PlainTextResponse(
        markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="report-{report.id}.md"'},
    )


def _empty_error_daily(days: int = 14) -> dict[str, dict]:
    today = _now().date()
    rows: dict[str, dict] = {}
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        rows[day.isoformat()] = {"date": day.isoformat(), "errors": 0}
    return rows


def _empty_daily(days: int = 14) -> dict[str, dict]:
    today = _now().date()
    rows: dict[str, dict] = {}
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        rows[day.isoformat()] = {
            "date": day.isoformat(),
            "pageviews": 0,
            "visitors": 0,
            "signups": 0,
            "activations": 0,
            "events": 0,
        }
    return rows


@router.post("/{pipeline_id}/usage-source")
async def create_usage_source(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    product = await _require_launched_product(pipeline_id, db, uid)
    existing = await _usage_source(product.id, db)
    if existing:
        return {"data": _serialize_usage_source(existing)}

    now = _now()
    source = MonitorUsageSource(
        id=str(uuid.uuid4()),
        pipeline_id=product.id,
        user_id=uid,
        public_key=secrets.token_urlsafe(24),
        status="connected",
        product_url=_clean_text(product.url),
        allowed_domain=_domain_from_url(product.url),
        created_at=now,
        updated_at=now,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return {"data": _serialize_usage_source(source)}


@router.patch("/{pipeline_id}/usage-source")
async def update_usage_source(
    pipeline_id: str,
    body: UsageSourceUpdateBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    product = await _require_launched_product(pipeline_id, db, uid)
    source = await _usage_source(product.id, db)
    if not source:
        raise HTTPException(status_code=404, detail="Usage source not found")

    source.product_url = _clean_text(body.product_url)
    source.allowed_domain = _domain_from_url(body.allowed_domain or body.product_url)
    source.updated_at = _now()
    await db.commit()
    await db.refresh(source)
    return {"data": _serialize_usage_source(source)}


def _resolve_identity(stripe_customer_id: str | None, email: str | None, customer_by_email: dict[str, str]) -> tuple[str | None, str]:
    """Resolve a usage actor to a Stripe customer. Explicit id wins; otherwise
    match email against the Stripe customer directory; otherwise unresolved.
    Returns ``(stripe_customer_id, resolution_method)``."""
    if stripe_customer_id:
        return stripe_customer_id, "explicit"
    if email:
        matched = customer_by_email.get(email.strip().lower())
        if matched:
            return matched, "email"
    return None, "unresolved"


async def _customer_email_index(db: AsyncSession, source_id: str) -> dict[str, str]:
    rows = (await db.execute(
        select(MonitorCustomer.stripe_customer_id, MonitorCustomer.email)
        .where(MonitorCustomer.revenue_source_id == source_id)
    )).all()
    return {email.strip().lower(): cid for cid, email in rows if email}


def _pct_change(current: int, previous: int) -> float | None:
    """Week-over-week change as a fraction, or None when there's no baseline."""
    if not previous:
        return None
    return round((current - previous) / previous, 4)


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _compute_retention(
    first_seen_rows: list[tuple],
    active_rows: list[tuple],
    today: date,
    window_days: int = 30,
    cohort_limit: int = 7,
) -> dict:
    """Day-1 / Day-7 return retention.

    A visitor is "retained DN" if they have any activity N days after their
    first-seen day. Only cohorts old enough to have been observed for N days
    count toward the rate, so a brand-new cohort doesn't drag it down.
    """
    active = {(visitor, _as_date(day)) for visitor, day in active_rows}
    overall = {1: {"eligible": 0, "retained": 0}, 7: {"eligible": 0, "retained": 0}}
    cohorts: dict[date, dict] = {}

    for visitor, day in first_seen_rows:
        first = _as_date(day)
        cohort = cohorts.setdefault(first, {"size": 0, 1: {"eligible": 0, "retained": 0}, 7: {"eligible": 0, "retained": 0}})
        cohort["size"] += 1
        for n in (1, 7):
            if first + timedelta(days=n) <= today:
                overall[n]["eligible"] += 1
                cohort[n]["eligible"] += 1
                if (visitor, first + timedelta(days=n)) in active:
                    overall[n]["retained"] += 1
                    cohort[n]["retained"] += 1

    def _bucket(agg: dict) -> dict:
        rate = round(agg["retained"] / agg["eligible"], 4) if agg["eligible"] else None
        return {"eligible": agg["eligible"], "retained": agg["retained"], "rate": rate}

    def _rate(agg: dict) -> float | None:
        return round(agg["retained"] / agg["eligible"], 4) if agg["eligible"] else None

    cohort_list = [
        {
            "date": cohort_date.isoformat(),
            "size": cohorts[cohort_date]["size"],
            "d1Rate": _rate(cohorts[cohort_date][1]),
            "d7Rate": _rate(cohorts[cohort_date][7]),
        }
        for cohort_date in sorted(cohorts.keys(), reverse=True)[:cohort_limit]
    ]

    return {
        "windowDays": window_days,
        "d1": _bucket(overall[1]),
        "d7": _bucket(overall[7]),
        "cohorts": cohort_list,
    }


@router.get("/{pipeline_id}/usage")
async def get_usage_metrics(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    now = _now()
    usage_window = eff.analytics_usage_window_days
    growth_window = eff.analytics_growth_window_days
    since = now - timedelta(days=usage_window)
    week_start = now - timedelta(days=growth_window)
    prev_week_start = now - timedelta(days=2 * growth_window)

    total_events = await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(MonitorUsageEvent.pipeline_id == pipeline_id)
    ) or 0

    window_rows = (await db.execute(
        select(MonitorUsageEvent.event_type, func.count(MonitorUsageEvent.id))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
        )
        .group_by(MonitorUsageEvent.event_type)
    )).all()
    counts = {event_type: int(count) for event_type, count in window_rows}

    visitors_14d = await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.visitor_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.visitor_id != None,
        )
    ) or 0
    active_users_14d = await db.scalar(
        select(func.count(func.distinct(func.coalesce(MonitorUsageEvent.user_ref, MonitorUsageEvent.visitor_id)))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
        )
    ) or 0

    daily = _empty_daily(usage_window)
    daily_rows = (await db.execute(
        select(
            func.date(MonitorUsageEvent.occurred_at),
            MonitorUsageEvent.event_type,
            func.count(MonitorUsageEvent.id),
            func.count(func.distinct(MonitorUsageEvent.visitor_id)),
        )
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
        )
        .group_by(func.date(MonitorUsageEvent.occurred_at), MonitorUsageEvent.event_type)
        .order_by(func.date(MonitorUsageEvent.occurred_at).asc())
    )).all()
    for day, event_type, count, visitors in daily_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if key not in daily:
            continue
        daily[key]["events"] += int(count)
        if event_type == "pageview":
            daily[key]["pageviews"] += int(count)
            daily[key]["visitors"] = max(daily[key]["visitors"], int(visitors or 0))
        elif event_type == "signup":
            daily[key]["signups"] += int(count)
        elif event_type == "activation":
            daily[key]["activations"] += int(count)

    recent_events = list((await db.execute(
        select(MonitorUsageEvent)
        .where(MonitorUsageEvent.pipeline_id == pipeline_id)
        .order_by(MonitorUsageEvent.occurred_at.desc())
        .limit(20)
    )).scalars().all())

    # Aggregation engine v0 — the rollups the screen leads with; the raw
    # recent-events log above is secondary. Top pages: pageviews grouped by URL.
    top_pages = [
        {
            "url": url,
            "views": int(views),
            "visitors": int(visitors or 0),
            "lastSeenAt": _iso(last_seen),
        }
        for url, views, visitors, last_seen in (await db.execute(
            select(
                MonitorUsageEvent.url,
                func.count(MonitorUsageEvent.id),
                func.count(func.distinct(MonitorUsageEvent.visitor_id)),
                func.max(MonitorUsageEvent.occurred_at),
            )
            .where(
                MonitorUsageEvent.pipeline_id == pipeline_id,
                MonitorUsageEvent.occurred_at >= since,
                MonitorUsageEvent.event_type == "pageview",
                MonitorUsageEvent.url != None,
            )
            .group_by(MonitorUsageEvent.url)
            .order_by(func.count(MonitorUsageEvent.id).desc())
            .limit(10)
        )).all()
    ]

    # Top events by frequency ranks actions, not pageviews. A custom event's name
    # lives in metadata.name; typed events (signup/login/activation) fall back to
    # their event_type. Pageviews are excluded — they're the top-pages rollup.
    event_name = func.coalesce(MonitorUsageEvent.event_metadata["name"].astext, MonitorUsageEvent.event_type)
    top_events = [
        {
            "name": name,
            "count": int(count),
            "visitors": int(visitors or 0),
            "lastSeenAt": _iso(last_seen),
        }
        for name, count, visitors, last_seen in (await db.execute(
            select(
                event_name.label("name"),
                func.count(MonitorUsageEvent.id),
                func.count(func.distinct(MonitorUsageEvent.visitor_id)),
                func.max(MonitorUsageEvent.occurred_at),
            )
            .where(
                MonitorUsageEvent.pipeline_id == pipeline_id,
                MonitorUsageEvent.occurred_at >= since,
                MonitorUsageEvent.event_type != "pageview",
            )
            .group_by(event_name)
            .order_by(func.count(MonitorUsageEvent.id).desc())
            .limit(10)
        )).all()
    ]

    # Conversion funnel (14d): distinct visitors at each stage, so a few noisy
    # visitors firing many events don't distort the rates.
    stage_rows = (await db.execute(
        select(MonitorUsageEvent.event_type, func.count(func.distinct(MonitorUsageEvent.visitor_id)))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.visitor_id != None,
            MonitorUsageEvent.event_type.in_(["signup", "activation"]),
        )
        .group_by(MonitorUsageEvent.event_type)
    )).all()
    stage_visitors = {event_type: int(count) for event_type, count in stage_rows}
    visited = int(visitors_14d)
    signed_up = stage_visitors.get("signup", 0)
    activated = stage_visitors.get("activation", 0)
    funnel = {
        "visited": visited,
        "signedUp": signed_up,
        "activated": activated,
        "signupRate": round(signed_up / visited, 4) if visited else None,
        "activationRate": round(activated / signed_up, 4) if signed_up else None,
    }

    # Week-over-week growth: this 7d window vs the prior 7d window.
    this_week_visitors = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.visitor_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= week_start,
            MonitorUsageEvent.visitor_id != None,
        )
    ) or 0)
    prev_week_visitors = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.visitor_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= prev_week_start,
            MonitorUsageEvent.occurred_at < week_start,
            MonitorUsageEvent.visitor_id != None,
        )
    ) or 0)
    this_week_signups = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= week_start,
            MonitorUsageEvent.event_type == "signup",
        )
    ) or 0)
    prev_week_signups = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= prev_week_start,
            MonitorUsageEvent.occurred_at < week_start,
            MonitorUsageEvent.event_type == "signup",
        )
    ) or 0)
    growth = {
        "visitors": {
            "current": this_week_visitors,
            "previous": prev_week_visitors,
            "changePct": _pct_change(this_week_visitors, prev_week_visitors),
        },
        "signups": {
            "current": this_week_signups,
            "previous": prev_week_signups,
            "changePct": _pct_change(this_week_signups, prev_week_signups),
        },
    }

    # Retention: pull each visitor's first-seen day and their distinct active
    # days over a 30d window, then compute D1/D7 return in-process.
    retention_window_days = eff.analytics_retention_window_days
    retention_since = now - timedelta(days=retention_window_days)
    first_seen_rows = (await db.execute(
        select(MonitorUsageEvent.visitor_id, func.min(func.date(MonitorUsageEvent.occurred_at)))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= retention_since,
            MonitorUsageEvent.visitor_id != None,
        )
        .group_by(MonitorUsageEvent.visitor_id)
    )).all()
    active_rows = (await db.execute(
        select(MonitorUsageEvent.visitor_id, func.date(MonitorUsageEvent.occurred_at))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= retention_since,
            MonitorUsageEvent.visitor_id != None,
        )
        .distinct()
    )).all()
    retention = _compute_retention(
        list(first_seen_rows), list(active_rows), now.date(), window_days=retention_window_days
    )

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "totalEvents": int(total_events),
            "lastSeenAt": _iso(source.last_seen_at if source else None),
            "health": _health_verdict(
                connected=_usage_source_is_connected(source, pipeline_id),
                last_seen_at=source.last_seen_at if source else None,
                total_events=int(total_events),
                eff=eff,
                now=now,
            ),
            "windowDays": usage_window,
            "growthWindowDays": growth_window,
            "summary14d": {
                "pageviews": counts.get("pageview", 0),
                "visitors": int(visitors_14d),
                "signups": counts.get("signup", 0),
                "logins": counts.get("login", 0),
                "activations": counts.get("activation", 0),
                "customEvents": counts.get("custom", 0),
                "activeUsers": int(active_users_14d),
            },
            "topPages": top_pages,
            "topEvents": top_events,
            "funnel": funnel,
            "growth": growth,
            "retention": retention,
            "daily": list(daily.values()),
            "recentEvents": [_serialize_usage_event(event) for event in recent_events],
        }
    }


@router.get("/{pipeline_id}/sessions")
async def get_sessions(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Sessions as first-class objects: each row is an aggregate of the events
    sharing a session_id (the B.2 identity payoff), not a raw event log. Derived
    from portfolio_usage_events — no sessions table until perf demands one."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    base = (
        MonitorUsageEvent.pipeline_id == pipeline_id,
        MonitorUsageEvent.occurred_at >= since,
        MonitorUsageEvent.session_id != None,
    )
    total_sessions = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.session_id))).where(*base)
    ) or 0)
    identified_sessions = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.session_id))).where(
            *base, MonitorUsageEvent.user_ref != None
        )
    ) or 0)
    total_events = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(*base)
    ) or 0)

    # The rollup: one aggregated row per session, most-recently-active first.
    pageviews = func.sum(case((MonitorUsageEvent.event_type == "pageview", 1), else_=0))
    rows = (await db.execute(
        select(
            MonitorUsageEvent.session_id,
            func.max(MonitorUsageEvent.visitor_id),
            func.max(MonitorUsageEvent.user_ref),
            func.min(MonitorUsageEvent.occurred_at),
            func.max(MonitorUsageEvent.occurred_at),
            func.count(MonitorUsageEvent.id),
            pageviews,
        )
        .where(*base)
        .group_by(MonitorUsageEvent.session_id)
        .order_by(func.max(MonitorUsageEvent.occurred_at).desc())
        .limit(50)
    )).all()

    sessions = []
    for session_id, visitor_id, user_ref, started, ended, events, page_count in rows:
        duration = int((ended - started).total_seconds()) if started and ended else 0
        sessions.append({
            "sessionId": session_id,
            "visitorId": visitor_id,
            "userRef": user_ref,
            "identified": user_ref is not None,
            "startedAt": _iso(started),
            "endedAt": _iso(ended),
            "durationSeconds": duration,
            "events": int(events),
            "pageviews": int(page_count or 0),
        })

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "summary": {
                "totalSessions": total_sessions,
                "identifiedSessions": identified_sessions,
                "avgEventsPerSession": round(total_events / total_sessions, 1) if total_sessions else None,
            },
            "sessions": sessions,
        }
    }


@router.get("/{pipeline_id}/sessions/{session_id}")
async def get_session_detail(
    pipeline_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """One session's timeline: its usage events and the errors that happened in
    it, merged in chronological order. Drill carries context — the error shows
    up in situ, next to what the visitor was doing when it fired."""
    await _require_launched_product(pipeline_id, db, uid)
    source = await _usage_source(pipeline_id, db)

    usage_events = list((await db.execute(
        select(MonitorUsageEvent)
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.session_id == session_id,
        )
        .order_by(MonitorUsageEvent.occurred_at.asc())
        .limit(500)
    )).scalars().all())
    error_events = list((await db.execute(
        select(MonitorErrorEvent)
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.session_id == session_id,
        )
        .order_by(MonitorErrorEvent.occurred_at.asc())
        .limit(500)
    )).scalars().all())

    if not usage_events and not error_events:
        raise HTTPException(status_code=404, detail="Session not found")

    items: list[tuple] = []
    for event in usage_events:
        meta = event.event_metadata or {}
        items.append((event.occurred_at, {
            "kind": "event",
            "id": event.id,
            "eventType": event.event_type,
            "name": meta.get("name"),
            "url": event.url,
            "metadata": meta,
            "occurredAt": _iso(event.occurred_at),
        }))
    for event in error_events:
        items.append((event.occurred_at, {
            "kind": "error",
            "id": event.id,
            "groupId": event.group_id,
            "fingerprint": event.fingerprint,
            "message": event.message,
            "level": event.level,
            "url": event.url,
            "occurredAt": _iso(event.occurred_at),
        }))
    items.sort(key=lambda item: item[0])
    timeline = [payload for _, payload in items]

    both = usage_events + error_events
    started = items[0][0]
    ended = items[-1][0]
    user_ref = next((e.user_ref for e in both if e.user_ref), None)
    visitor_id = next((e.visitor_id for e in both if e.visitor_id), None)

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "session": {
                "sessionId": session_id,
                "visitorId": visitor_id,
                "userRef": user_ref,
                "identified": user_ref is not None,
                "startedAt": _iso(started),
                "endedAt": _iso(ended),
                "durationSeconds": int((ended - started).total_seconds()) if started and ended else 0,
                "events": len(usage_events),
                "pageviews": sum(1 for e in usage_events if e.event_type == "pageview"),
                "errors": len(error_events),
            },
            "timeline": timeline,
        }
    }


def _series_stats(values: list[int]) -> dict:
    """Normal-range band for the hero chart: mean +/- 1 stddev over the window
    (lower clamped at 0). A point outside the band is the 'this is abnormal' cue."""
    if not values:
        return {"mean": 0.0, "lower": 0.0, "upper": 0.0}
    n = len(values)
    mean = sum(values) / n
    std = (sum((v - mean) ** 2 for v in values) / n) ** 0.5
    return {"mean": round(mean, 2), "lower": round(max(0.0, mean - std), 2), "upper": round(mean + std, 2)}


@router.get("/{pipeline_id}/timeseries")
async def get_timeseries(
    pipeline_id: str,
    metric: Literal["errors", "loads"] = "errors",
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Over-time hero chart for one metric: daily points over the window, a
    normal-range baseline band (mean +/- stddev), and deploy markers (the first
    day each release appeared) so a spike can be read against 'what's normal' and
    lined up with the deploy that caused it."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)
    keys = sorted(_empty_daily(usage_window).keys())

    if metric == "errors":
        series_rows = (await db.execute(
            select(func.date(MonitorErrorEvent.occurred_at), func.count(MonitorErrorEvent.id))
            .where(MonitorErrorEvent.pipeline_id == pipeline_id, MonitorErrorEvent.occurred_at >= since)
            .group_by(func.date(MonitorErrorEvent.occurred_at))
        )).all()
    else:
        series_rows = (await db.execute(
            select(func.date(MonitorUsageEvent.occurred_at), func.count(MonitorUsageEvent.id))
            .where(
                MonitorUsageEvent.pipeline_id == pipeline_id,
                MonitorUsageEvent.occurred_at >= since,
                MonitorUsageEvent.event_type == "pageview",
            )
            .group_by(func.date(MonitorUsageEvent.occurred_at))
        )).all()
    counts: dict[str, int] = {}
    for day, count in series_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        counts[key] = int(count)
    points = [{"date": key, "value": counts.get(key, 0)} for key in keys]
    baseline = _series_stats([p["value"] for p in points])

    # Deploy markers: the first day each release was seen (a release rollout).
    marker_rows = (await db.execute(
        select(MonitorUsageEvent.release, func.min(func.date(MonitorUsageEvent.occurred_at)))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.release != None,
        )
        .group_by(MonitorUsageEvent.release)
    )).all()
    markers = []
    for release, first_day in marker_rows:
        key = first_day.isoformat() if hasattr(first_day, "isoformat") else str(first_day)
        markers.append({"date": key, "release": release})
    markers.sort(key=lambda m: m["date"])

    return {
        "data": {
            "metric": metric,
            "windowDays": usage_window,
            "points": points,
            "baseline": baseline,
            "markers": markers,
        }
    }


def _explorer_health(error_rate: float | None, lcp_rating: str | None, loads: int) -> str:
    """One-glance verdict per page: error rate dominates, then felt-speed."""
    if not loads:
        return "no-data"
    if error_rate is not None and error_rate >= 0.05:
        return "unhealthy"
    if lcp_rating == "poor" or (error_rate is not None and error_rate >= 0.01):
        return "warning"
    return "healthy"


@router.get("/{pipeline_id}/explorer")
async def get_explorer(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Multi-metric explorer: one row per page with loads, error rate, felt-speed
    (LCP p75 + rating), a health badge, and a per-row loads sparkline — the
    sortable 'where do I look first' table."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)
    window_dates = sorted(_empty_daily(usage_window).keys())

    load_rows = (await db.execute(
        select(MonitorUsageEvent.url, func.count(MonitorUsageEvent.id))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.event_type == "pageview",
            MonitorUsageEvent.url != None,
        )
        .group_by(MonitorUsageEvent.url)
        .order_by(func.count(MonitorUsageEvent.id).desc())
        .limit(25)
    )).all()
    urls = [url for url, _ in load_rows]

    error_rows = (await db.execute(
        select(MonitorErrorEvent.url, func.count(MonitorErrorEvent.id))
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.url.in_(urls) if urls else False,
        )
        .group_by(MonitorErrorEvent.url)
    )).all()
    errors_by_url = {url: int(count) for url, count in error_rows}

    lcp_rows = (await db.execute(
        select(
            MonitorWebVital.url,
            func.percentile_cont(0.75).within_group(MonitorWebVital.value.asc()),
        )
        .where(
            MonitorWebVital.pipeline_id == pipeline_id,
            MonitorWebVital.occurred_at >= since,
            MonitorWebVital.metric == "LCP",
            MonitorWebVital.url.in_(urls) if urls else False,
        )
        .group_by(MonitorWebVital.url)
    )).all()
    lcp_by_url = {url: p75 for url, p75 in lcp_rows}

    spark_rows = (await db.execute(
        select(MonitorUsageEvent.url, func.date(MonitorUsageEvent.occurred_at), func.count(MonitorUsageEvent.id))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.event_type == "pageview",
            MonitorUsageEvent.url.in_(urls) if urls else False,
        )
        .group_by(MonitorUsageEvent.url, func.date(MonitorUsageEvent.occurred_at))
    )).all()
    spark_by_url: dict[str, dict[str, int]] = {}
    for url, day, count in spark_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        spark_by_url.setdefault(url, {})[key] = int(count)

    rows = []
    for url, loads in load_rows:
        loads = int(loads)
        errors = errors_by_url.get(url, 0)
        error_rate = round(errors / loads, 4) if loads else None
        lcp_p75 = _round_vital("LCP", lcp_by_url.get(url))
        lcp_rating = _rate_vital("LCP", lcp_p75)
        daily = spark_by_url.get(url, {})
        rows.append({
            "url": url,
            "loads": loads,
            "errors": errors,
            "errorRate": error_rate,
            "lcpP75": lcp_p75,
            "lcpRating": lcp_rating,
            "health": _explorer_health(error_rate, lcp_rating, loads),
            "spark": [daily.get(d, 0) for d in window_dates],
        })

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "rows": rows,
        }
    }


@router.get("/{pipeline_id}/traces/{trace_id}")
async def get_trace(
    pipeline_id: str,
    trace_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """One trace, assembled into the incident chain: the span tree (client root →
    fetch → server) with the errors and logs that share the trace_id pinned to
    it, so a frontend error can be followed to the backend work that caused it."""
    await _require_launched_product(pipeline_id, db, uid)

    spans = list((await db.execute(
        select(MonitorSpan)
        .where(MonitorSpan.pipeline_id == pipeline_id, MonitorSpan.trace_id == trace_id)
        .order_by(MonitorSpan.start_at.asc())
    )).scalars().all())
    errors = list((await db.execute(
        select(MonitorErrorEvent)
        .where(MonitorErrorEvent.pipeline_id == pipeline_id, MonitorErrorEvent.trace_id == trace_id)
        .order_by(MonitorErrorEvent.occurred_at.asc())
    )).scalars().all())
    logs = list((await db.execute(
        select(MonitorLog)
        .where(MonitorLog.pipeline_id == pipeline_id, MonitorLog.trace_id == trace_id)
        .order_by(MonitorLog.occurred_at.asc())
    )).scalars().all())

    span_payloads = [_serialize_span(span, depth) for span, depth in _order_span_tree(spans)]
    services: list[str] = []
    for span in spans:
        if span.service and span.service not in services:
            services.append(span.service)
    durations = [s.duration_ms for s in spans if s.duration_ms is not None]

    return {
        "data": {
            "traceId": trace_id,
            "spans": span_payloads,
            "errors": [_serialize_error_event(e) for e in errors],
            "logs": [{
                "id": log.id,
                "level": log.level,
                "message": log.message,
                "url": log.url,
                "spanId": log.span_id,
                "occurredAt": _iso(log.occurred_at),
            } for log in logs],
            "summary": {
                "spanCount": len(spans),
                "errorCount": len(errors),
                "logCount": len(logs),
                "services": services,
                "hasServer": any(s.kind == "server" for s in spans),
                "durationMs": max(durations) if durations else None,
            },
        }
    }


def _flow_graph(rows, *, max_nodes: int = 30, max_edges: int = 50) -> tuple[list, list]:
    """Build the journey graph from pageviews ordered by (session, time): node =
    page with its visit count, edge = a consecutive page→page hop within one
    session (self-hops/refreshes dropped). Edges are kept only between the top
    nodes so the graph stays readable."""
    visits: dict[str, int] = {}
    edges: dict[tuple, int] = {}
    prev_session = None
    prev_url = None
    for session_id, url, _ in rows:
        visits[url] = visits.get(url, 0) + 1
        if session_id == prev_session and prev_url is not None and prev_url != url:
            key = (prev_url, url)
            edges[key] = edges.get(key, 0) + 1
        prev_session = session_id
        prev_url = url

    top = sorted(visits.items(), key=lambda kv: kv[1], reverse=True)[:max_nodes]
    node_set = {url for url, _ in top}
    nodes = [{"url": url, "visits": count} for url, count in top]
    edge_list = [
        {"from": a, "to": b, "count": count}
        for (a, b), count in sorted(edges.items(), key=lambda kv: kv[1], reverse=True)
        if a in node_set and b in node_set
    ][:max_edges]
    return nodes, edge_list


@router.get("/{pipeline_id}/flow")
async def get_flow(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Journey / flow graph: how one page leads into the next, aggregated from
    the ordered pageviews within each session — the top entry pages and the most
    common page→page paths."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    rows = (await db.execute(
        select(MonitorUsageEvent.session_id, MonitorUsageEvent.url, MonitorUsageEvent.occurred_at)
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.event_type == "pageview",
            MonitorUsageEvent.session_id != None,
            MonitorUsageEvent.url != None,
        )
        .order_by(MonitorUsageEvent.session_id.asc(), MonitorUsageEvent.occurred_at.asc())
        .limit(5000)
    )).all()
    nodes, edges = _flow_graph(rows)

    return {
        "data": {
            "windowDays": usage_window,
            "nodes": nodes,
            "edges": edges,
        }
    }


def _feature_flow(rows, *, max_nodes: int = 30, max_edges: int = 50) -> tuple[list, list]:
    """Build the flow graph from feature spans ordered by (session, time): node =
    a named feature (Sign Up, Checkout) with its run count, error count and mean
    duration; edge = a consecutive feature→feature hop within one session. Unlike
    the URL graph this measures outcomes — how often a flow ran, how slow it was,
    and how often it failed — so Monitor can show feature names, not pages."""
    stats: dict[str, dict] = {}
    edges: dict[tuple, int] = {}
    prev_session = None
    prev_feature = None
    for session_id, feature, status, duration_ms, _ in rows:
        stat = stats.setdefault(feature, {"count": 0, "errors": 0, "dur_sum": 0.0, "dur_n": 0})
        stat["count"] += 1
        if status == "error":
            stat["errors"] += 1
        if duration_ms is not None:
            stat["dur_sum"] += duration_ms
            stat["dur_n"] += 1
        if session_id == prev_session and prev_feature is not None and prev_feature != feature:
            key = (prev_feature, feature)
            edges[key] = edges.get(key, 0) + 1
        prev_session = session_id
        prev_feature = feature

    top = sorted(stats.items(), key=lambda kv: kv[1]["count"], reverse=True)[:max_nodes]
    node_set = {feature for feature, _ in top}
    nodes = [
        {
            "feature": feature,
            "count": stat["count"],
            "errorCount": stat["errors"],
            "avgDurationMs": round(stat["dur_sum"] / stat["dur_n"], 1) if stat["dur_n"] else None,
        }
        for feature, stat in top
    ]
    edge_list = [
        {"from": a, "to": b, "count": count}
        for (a, b), count in sorted(edges.items(), key=lambda kv: kv[1], reverse=True)
        if a in node_set and b in node_set
    ][:max_edges]
    return nodes, edge_list


@router.get("/{pipeline_id}/feature-flow")
async def get_feature_flow(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Feature-flow graph: the named user flows (from instrumented feature spans)
    and how one leads into the next, with run count, error count and mean
    duration per feature. Empty until flows are instrumented — the UI falls back
    to the URL flow in that case."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    rows = (await db.execute(
        select(MonitorSpan.session_id, MonitorSpan.feature, MonitorSpan.status, MonitorSpan.duration_ms, MonitorSpan.start_at)
        .where(
            MonitorSpan.pipeline_id == pipeline_id,
            MonitorSpan.start_at >= since,
            MonitorSpan.feature != None,
            MonitorSpan.session_id != None,
        )
        .order_by(MonitorSpan.session_id.asc(), MonitorSpan.start_at.asc())
        .limit(5000)
    )).all()
    nodes, edges = _feature_flow(rows)

    return {
        "data": {
            "windowDays": usage_window,
            "nodes": nodes,
            "edges": edges,
        }
    }


def _round_vital(metric: str, value) -> float | None:
    if value is None:
        return None
    # CLS is a small unitless score; keep precision. The rest are milliseconds.
    return round(float(value), 3 if metric == "CLS" else 1)


@router.get("/{pipeline_id}/experience")
async def get_experience_vitals(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Experience vitals rollup: per-metric p75 + rating distribution, and the
    same per URL. Leads with the verdict (p75 → good/needs-improvement/poor), not
    the raw samples."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    metric_rows = (await db.execute(
        select(
            MonitorWebVital.metric,
            func.count(MonitorWebVital.id),
            func.percentile_cont(0.75).within_group(MonitorWebVital.value.asc()),
        )
        .where(MonitorWebVital.pipeline_id == pipeline_id, MonitorWebVital.occurred_at >= since)
        .group_by(MonitorWebVital.metric)
    )).all()

    rating_rows = (await db.execute(
        select(MonitorWebVital.metric, MonitorWebVital.rating, func.count(MonitorWebVital.id))
        .where(MonitorWebVital.pipeline_id == pipeline_id, MonitorWebVital.occurred_at >= since)
        .group_by(MonitorWebVital.metric, MonitorWebVital.rating)
    )).all()

    url_rows = (await db.execute(
        select(
            MonitorWebVital.url,
            MonitorWebVital.metric,
            func.count(MonitorWebVital.id),
            func.percentile_cont(0.75).within_group(MonitorWebVital.value.asc()),
        )
        .where(
            MonitorWebVital.pipeline_id == pipeline_id,
            MonitorWebVital.occurred_at >= since,
            MonitorWebVital.url != None,
        )
        .group_by(MonitorWebVital.url, MonitorWebVital.metric)
        .order_by(func.count(MonitorWebVital.id).desc())
        .limit(60)
    )).all()

    ratings: dict[str, dict] = {}
    for metric, rating, count in rating_rows:
        bucket = ratings.setdefault(metric, {"good": 0, "needs-improvement": 0, "poor": 0})
        if rating in bucket:
            bucket[rating] = int(count)

    metrics = []
    for metric, count, p75 in metric_rows:
        value = _round_vital(metric, p75)
        bucket = ratings.get(metric, {})
        metrics.append({
            "metric": metric,
            "sampleCount": int(count),
            "p75": value,
            "rating": _rate_vital(metric, value),
            "good": bucket.get("good", 0),
            "needsImprovement": bucket.get("needs-improvement", 0),
            "poor": bucket.get("poor", 0),
        })
    metrics.sort(key=lambda m: _VITAL_ORDER.get(m["metric"], 99))

    pages: dict[str, dict] = {}
    for url, metric, count, p75 in url_rows:
        page = pages.setdefault(url, {"url": url, "sampleCount": 0, "metrics": {}})
        page["sampleCount"] += int(count)
        value = _round_vital(metric, p75)
        page["metrics"][metric] = {"p75": value, "rating": _rate_vital(metric, value)}
    pages_list = sorted(pages.values(), key=lambda p: p["sampleCount"], reverse=True)[:10]

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "totalSamples": sum(m["sampleCount"] for m in metrics),
            "metrics": metrics,
            "pages": pages_list,
        }
    }


@router.get("/{pipeline_id}/logs")
async def get_logs(
    pipeline_id: str,
    level: LogLevel | None = None,
    q: str | None = Query(default=None, max_length=200),
    session: str | None = None,
    release: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Faceted log explorer: leads with the per-level counts (the rollup), then
    the filtered lines. Facets — level, message search (q), session, release —
    compose; the level counts reflect the other filters so the facet is live."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    # Filters other than level apply to both the level facet and the lines, so
    # the facet shows the level distribution within the current context.
    base = [MonitorLog.pipeline_id == pipeline_id, MonitorLog.occurred_at >= since]
    if q:
        base.append(MonitorLog.message.ilike(f"%{q}%"))
    if session:
        base.append(MonitorLog.session_id == session)
    if release:
        base.append(MonitorLog.release == release)

    level_rows = (await db.execute(
        select(MonitorLog.level, func.count(MonitorLog.id)).where(*base).group_by(MonitorLog.level)
    )).all()
    level_counts = {lvl: 0 for lvl in ("debug", "info", "warn", "error")}
    for lvl, count in level_rows:
        if lvl in level_counts:
            level_counts[lvl] = int(count)

    line_filters = list(base)
    if level:
        line_filters.append(MonitorLog.level == level)
    logs = list((await db.execute(
        select(MonitorLog)
        .where(*line_filters)
        .order_by(MonitorLog.occurred_at.desc())
        .limit(limit)
    )).scalars().all())

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "levelCounts": level_counts,
            "filters": {"level": level, "q": q, "session": session, "release": release},
            "logs": [
                {
                    "id": log.id,
                    "level": log.level,
                    "message": log.message,
                    "url": log.url,
                    "sessionId": log.session_id,
                    "userRef": log.user_ref,
                    "release": log.release,
                    "metadata": log.event_metadata or {},
                    "occurredAt": _iso(log.occurred_at),
                }
                for log in logs
            ],
        }
    }


def _build_correlation_days(usage_rows, error_rows, days: int = 30, today: date | None = None) -> list[dict]:
    """Merge usage + error daily rows into one aligned timeline."""
    today = today or _now().date()
    skeleton: dict[str, dict] = {}
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        skeleton[day.isoformat()] = {"date": day.isoformat(), "visitors": 0, "pageviews": 0, "signups": 0, "errors": 0}

    for day, event_type, count, visitors in usage_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if key not in skeleton:
            continue
        if event_type == "pageview":
            skeleton[key]["pageviews"] += int(count or 0)
            skeleton[key]["visitors"] = max(skeleton[key]["visitors"], int(visitors or 0))
        elif event_type == "signup":
            skeleton[key]["signups"] += int(count or 0)

    for day, count in error_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if key in skeleton:
            skeleton[key]["errors"] += int(count or 0)

    return list(skeleton.values())


def _correlation_insights(days: list[dict]) -> list[str]:
    """Plain-language callouts where signals move together — the cross-source read."""
    if not days:
        return []
    errors = [d["errors"] for d in days]
    signups = [d["signups"] for d in days]
    avg_err = sum(errors) / len(errors) if errors else 0
    avg_signups = sum(signups) / len(signups) if signups else 0

    insights: list[str] = []
    for day in reversed(days):  # most recent first
        if day["errors"] >= 5 and avg_err > 0 and day["errors"] >= 2 * avg_err:
            msg = f"Errors spiked to {day['errors']} on {day['date']}"
            if day["signups"] <= avg_signups:
                msg += f", while signups dipped to {day['signups']}"
            insights.append(msg + ".")
        if len(insights) >= 3:
            break
    return insights


# NOTE: this is the usage-vs-errors correlation. It is slated for a later rename
# to `/insights/error-correlation` to disambiguate from the new usage↔revenue
# correlation (`/insights/revenue-correlation`). Left at its current path here to
# keep this build non-breaking.
@router.get("/{pipeline_id}/correlation")
@router.get("/{pipeline_id}/insights/error-correlation")
async def get_correlation(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    now = _now()
    correlation_window = eff.analytics_correlation_window_days
    growth_window = eff.analytics_growth_window_days
    usage_window = eff.analytics_usage_window_days
    since = now - timedelta(days=correlation_window)
    week_start = now - timedelta(days=growth_window)
    prev_week_start = now - timedelta(days=2 * growth_window)
    usage_window_ago = now - timedelta(days=usage_window)

    usage_rows = (await db.execute(
        select(
            func.date(MonitorUsageEvent.occurred_at),
            MonitorUsageEvent.event_type,
            func.count(MonitorUsageEvent.id),
            func.count(func.distinct(MonitorUsageEvent.visitor_id)),
        )
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
        )
        .group_by(func.date(MonitorUsageEvent.occurred_at), MonitorUsageEvent.event_type)
    )).all()
    error_rows = (await db.execute(
        select(func.date(MonitorErrorEvent.occurred_at), func.count(MonitorErrorEvent.id))
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
        .group_by(func.date(MonitorErrorEvent.occurred_at))
    )).all()

    days = _build_correlation_days(list(usage_rows), list(error_rows), days=correlation_window, today=now.date())
    insights = _correlation_insights(days)

    revenue = await db.scalar(
        select(MonitorRevenueSource).where(
            MonitorRevenueSource.pipeline_id == pipeline_id,
            MonitorRevenueSource.provider == "stripe",
        )
    )
    this_week_signups = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.event_type == "signup",
            MonitorUsageEvent.occurred_at >= week_start,
        )
    ) or 0)
    prev_week_signups = int(await db.scalar(
        select(func.count(MonitorUsageEvent.id)).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.event_type == "signup",
            MonitorUsageEvent.occurred_at >= prev_week_start,
            MonitorUsageEvent.occurred_at < week_start,
        )
    ) or 0)
    errors_14d = int(await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= usage_window_ago,
        )
    ) or 0)
    sessions_14d = int(await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.session_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= usage_window_ago,
            MonitorUsageEvent.session_id != None,
        )
    ) or 0)

    mrr, prev_mrr = _selected_mrr(revenue, eff.revenue_engine)

    return {
        "data": {
            "days": days,
            "insights": insights,
            "windowDays": correlation_window,
            "summary": {
                "mrrCents": mrr,
                "mrrChangePct": _pct_change(mrr, prev_mrr) if (mrr is not None and prev_mrr) else None,
                "signupsChangePct": _pct_change(this_week_signups, prev_week_signups),
                "errorsPerSession14d": round(errors_14d / sessions_14d, 4) if sessions_14d else None,
                "errorsPerSessionWindowDays": usage_window,
            },
        }
    }


def _serialize_alert_settings(prefs: AlertPrefs) -> dict:
    return {
        "newIssueEnabled": prefs.new_issue_enabled,
        "errorSpikeEnabled": prefs.error_spike_enabled,
        "signupsDropEnabled": prefs.signups_drop_enabled,
        "revenueDropEnabled": prefs.revenue_drop_enabled,
        "errorSpikeMultiplier": prefs.error_spike_multiplier,
        "signupsDropPct": prefs.signups_drop_pct,
        "revenueDropPct": prefs.revenue_drop_pct,
    }


@router.get("/{pipeline_id}/alert-settings")
async def get_alert_settings(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    row = await alert_settings_row(db, pipeline_id)
    return {"data": _serialize_alert_settings(AlertPrefs.resolve(row))}


@router.put("/{pipeline_id}/alert-settings")
async def update_alert_settings(
    pipeline_id: str,
    body: AlertSettingsBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    row = await alert_settings_row(db, pipeline_id)
    if row is None:
        row = MonitorAlertSettings(pipeline_id=pipeline_id)
        db.add(row)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    row.updated_at = _now()
    await db.commit()
    await db.refresh(row)
    return {"data": _serialize_alert_settings(AlertPrefs.resolve(row))}


def _error_dimension_filters(pipeline_id: str, since, *, error_type: str | None, platform: str | None) -> list:
    """Group-level WHERE conditions for the error_type / platform dimension
    filters, shared by the errors + issues lists. error_type is stored on the
    group; platform lives on events, so a platform filter keeps groups that have
    at least one occurrence on that platform in the window."""
    conditions = []
    if error_type:
        conditions.append(MonitorErrorGroup.error_type == error_type)
    if platform:
        conditions.append(MonitorErrorGroup.id.in_(
            select(MonitorErrorEvent.group_id).where(
                MonitorErrorEvent.pipeline_id == pipeline_id,
                MonitorErrorEvent.platform == platform,
                MonitorErrorEvent.occurred_at >= since,
            ).distinct()
        ))
    return conditions


async def _error_dimension_facets(pipeline_id: str, since, db: AsyncSession) -> dict:
    """Available error_type / platform values (with counts) over the window, for
    the filter chips. Computed independent of the active filter so a chip never
    disappears just because it's selected. error_type is group-grained; platform
    is event-grained, counted as the distinct groups seen on each platform."""
    error_type_rows = (await db.execute(
        select(MonitorErrorGroup.error_type, func.count(MonitorErrorGroup.id))
        .where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.last_seen_at >= since,
            MonitorErrorGroup.error_type.isnot(None),
        )
        .group_by(MonitorErrorGroup.error_type)
        .order_by(func.count(MonitorErrorGroup.id).desc())
    )).all()
    platform_rows = (await db.execute(
        select(MonitorErrorEvent.platform, func.count(func.distinct(MonitorErrorEvent.group_id)))
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.platform.isnot(None),
        )
        .group_by(MonitorErrorEvent.platform)
        .order_by(func.count(func.distinct(MonitorErrorEvent.group_id)).desc())
    )).all()
    return {
        "errorType": [{"value": v, "count": int(c)} for v, c in error_type_rows],
        "platform": [{"value": v, "count": int(c)} for v, c in platform_rows],
    }


@router.get("/{pipeline_id}/errors")
async def get_error_metrics(
    pipeline_id: str,
    error_type: str | None = Query(default=None, alias="errorType", max_length=40),
    platform: str | None = Query(default=None, max_length=20),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    # The dimension filters ("choose what you want to see") scope the issue list
    # and recent-error feed; the health headline + trend stay global so the
    # filter narrows detail without hiding the product's overall error picture.
    event_filters = []
    if error_type:
        event_filters.append(MonitorErrorEvent.error_type == error_type)
    if platform:
        event_filters.append(MonitorErrorEvent.platform == platform)

    total_errors = await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(MonitorErrorEvent.pipeline_id == pipeline_id)
    ) or 0
    errors_14d = await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
    ) or 0
    affected_sessions_14d = await db.scalar(
        select(func.count(func.distinct(MonitorErrorEvent.session_id))).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.session_id != None,
        )
    ) or 0
    # Denominator for a rate (not a raw count): a traffic spike shouldn't read
    # as an error spike. Uses the usage sessions already collected for the product.
    usage_sessions_14d = await db.scalar(
        select(func.count(func.distinct(MonitorUsageEvent.session_id))).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.session_id != None,
        )
    ) or 0
    open_issues = await db.scalar(
        select(func.count(MonitorErrorGroup.id)).where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.status == "unresolved",
        )
    ) or 0

    daily = _empty_error_daily(usage_window)
    daily_rows = (await db.execute(
        select(func.date(MonitorErrorEvent.occurred_at), func.count(MonitorErrorEvent.id))
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
        .group_by(func.date(MonitorErrorEvent.occurred_at))
        .order_by(func.date(MonitorErrorEvent.occurred_at).asc())
    )).all()
    for day, count in daily_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if key in daily:
            daily[key]["errors"] += int(count)

    top_groups = list((await db.execute(
        select(MonitorErrorGroup)
        .where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.last_seen_at >= since,
            *_error_dimension_filters(pipeline_id, since, error_type=error_type, platform=platform),
        )
        .order_by(MonitorErrorGroup.event_count.desc(), MonitorErrorGroup.last_seen_at.desc())
        .limit(10)
    )).scalars().all())

    session_rows = (await db.execute(
        select(MonitorErrorEvent.group_id, func.count(func.distinct(MonitorErrorEvent.session_id)))
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.session_id != None,
        )
        .group_by(MonitorErrorEvent.group_id)
    )).all()
    sessions_by_group = {group_id: int(count) for group_id, count in session_rows}

    recent_errors = list((await db.execute(
        select(MonitorErrorEvent)
        .where(MonitorErrorEvent.pipeline_id == pipeline_id, *event_filters)
        .order_by(MonitorErrorEvent.occurred_at.desc())
        .limit(20)
    )).scalars().all())

    errors_per_session = round(int(errors_14d) / int(usage_sessions_14d), 4) if usage_sessions_14d else None

    facets = await _error_dimension_facets(pipeline_id, since, db)

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "totalErrors": int(total_errors),
            "lastSeenAt": _iso(source.last_seen_at if source else None),
            "windowDays": usage_window,
            "summary14d": {
                "errors": int(errors_14d),
                "openIssues": int(open_issues),
                "affectedSessions": int(affected_sessions_14d),
                "errorsPerSession": errors_per_session,
            },
            "daily": list(daily.values()),
            "issues": [
                _serialize_error_group(group, affected_sessions=sessions_by_group.get(group.id, 0))
                for group in top_groups
            ],
            "recentErrors": [_serialize_error_event(event) for event in recent_errors],
            "facets": facets,
            "filters": {"errorType": error_type, "platform": platform},
        }
    }


def _issue_trend(recent: int, prior: int) -> dict:
    """Direction of an issue over the window: this half vs the prior half. 'up'
    means it's getting worse (a regression signal), 'down' means it's receding."""
    if recent > prior:
        direction = "up"
    elif recent < prior:
        direction = "down"
    else:
        direction = "flat"
    change_pct = _pct_change(recent, prior)
    return {"direction": direction, "recent": int(recent), "prior": int(prior), "changePct": change_pct}


def _serialize_issue(group: MonitorErrorGroup, *, affected_users, affected_sessions, occurrences, trend) -> dict:
    """A fingerprint group promoted to an issue object: the group's identity plus
    the impact metrics the engine computes (affected users/sessions, windowed
    occurrences, trend). affected-users is net-new — it isn't stored anywhere."""
    return {
        "id": group.id,
        "fingerprint": group.fingerprint,
        "title": group.title,
        "level": group.level,
        "status": group.status,
        "errorType": group.error_type,
        "lastRelease": group.last_release,
        "firstSeenAt": _iso(group.first_seen_at),
        "lastSeenAt": _iso(group.last_seen_at),
        "totalOccurrences": int(group.event_count or 0),
        "occurrences": int(occurrences),
        "affectedUsers": int(affected_users),
        "affectedSessions": int(affected_sessions),
        "trend": trend,
    }


@router.get("/{pipeline_id}/issues")
async def get_issues(
    pipeline_id: str,
    error_type: str | None = Query(default=None, alias="errorType", max_length=40),
    platform: str | None = Query(default=None, max_length=20),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Issue promotion engine: fingerprint groups → issue objects ranked by
    affected users. Affected-users/sessions and the trend are computed here from
    the raw error events (the group row only stores occurrences + first/last
    seen)."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    now = _now()
    usage_window = eff.analytics_usage_window_days
    since = now - timedelta(days=usage_window)
    midpoint = now - timedelta(days=usage_window / 2)
    actor = func.coalesce(MonitorErrorEvent.user_ref, MonitorErrorEvent.visitor_id)

    open_issues = int(await db.scalar(
        select(func.count(MonitorErrorGroup.id)).where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.status == "unresolved",
            MonitorErrorGroup.last_seen_at >= since,
        )
    ) or 0)
    affected_users_total = int(await db.scalar(
        select(func.count(func.distinct(actor))).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
    ) or 0)
    occurrences_total = int(await db.scalar(
        select(func.count(MonitorErrorEvent.id)).where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
        )
    ) or 0)

    # One pass over the window's events → per-group impact + a split into this
    # half / prior half for the trend.
    agg_rows = (await db.execute(
        select(
            MonitorErrorEvent.group_id,
            func.count(func.distinct(actor)),
            func.count(func.distinct(MonitorErrorEvent.session_id)),
            func.count(MonitorErrorEvent.id),
            func.sum(case((MonitorErrorEvent.occurred_at >= midpoint, 1), else_=0)),
            func.sum(case((MonitorErrorEvent.occurred_at < midpoint, 1), else_=0)),
        )
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.group_id != None,
        )
        .group_by(MonitorErrorEvent.group_id)
    )).all()
    agg = {
        group_id: (int(users), int(sessions), int(occ), int(recent or 0), int(prior or 0))
        for group_id, users, sessions, occ, recent, prior in agg_rows
    }

    groups = list((await db.execute(
        select(MonitorErrorGroup)
        .where(
            MonitorErrorGroup.pipeline_id == pipeline_id,
            MonitorErrorGroup.last_seen_at >= since,
            *_error_dimension_filters(pipeline_id, since, error_type=error_type, platform=platform),
        )
        .limit(200)
    )).scalars().all())

    issues = []
    for group in groups:
        users, sessions, occ, recent, prior = agg.get(group.id, (0, 0, 0, 0, 0))
        issues.append(_serialize_issue(
            group,
            affected_users=users,
            affected_sessions=sessions,
            occurrences=occ,
            trend=_issue_trend(recent, prior),
        ))
    # Ranked by affected users — the impact ordering the issues list leads with.
    issues.sort(key=lambda issue: (issue["affectedUsers"], issue["occurrences"]), reverse=True)
    issues = issues[:50]

    facets = await _error_dimension_facets(pipeline_id, since, db)

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "summary": {
                "openIssues": open_issues,
                "affectedUsers": affected_users_total,
                "occurrences": occurrences_total,
            },
            "issues": issues,
            "facets": facets,
            "filters": {"errorType": error_type, "platform": platform},
        }
    }


@router.get("/{pipeline_id}/issues/{group_id}")
async def get_issue_detail(
    pipeline_id: str,
    group_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """One issue, interrogable: the promoted issue object plus faceted breakdowns
    (by release, by URL) so you can pivot on 'which deploy / which page', a sample
    stack, and recent occurrences."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    now = _now()
    usage_window = eff.analytics_usage_window_days
    since = now - timedelta(days=usage_window)
    midpoint = now - timedelta(days=usage_window / 2)
    actor = func.coalesce(MonitorErrorEvent.user_ref, MonitorErrorEvent.visitor_id)

    group = (await db.execute(
        select(MonitorErrorGroup).where(
            MonitorErrorGroup.id == group_id,
            MonitorErrorGroup.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Issue not found")

    scoped = (
        MonitorErrorEvent.pipeline_id == pipeline_id,
        MonitorErrorEvent.group_id == group_id,
        MonitorErrorEvent.occurred_at >= since,
    )
    agg = (await db.execute(
        select(
            func.count(func.distinct(actor)),
            func.count(func.distinct(MonitorErrorEvent.session_id)),
            func.count(MonitorErrorEvent.id),
            func.sum(case((MonitorErrorEvent.occurred_at >= midpoint, 1), else_=0)),
            func.sum(case((MonitorErrorEvent.occurred_at < midpoint, 1), else_=0)),
        ).where(*scoped)
    )).all()
    users, sessions, occ, recent, prior = (
        (int(agg[0][0] or 0), int(agg[0][1] or 0), int(agg[0][2] or 0), int(agg[0][3] or 0), int(agg[0][4] or 0))
        if agg else (0, 0, 0, 0, 0)
    )

    # Facets: slice this issue's occurrences by a dimension. Pivoting is the verb.
    release_label = func.coalesce(MonitorErrorEvent.release, "(none)")
    release_rows = (await db.execute(
        select(release_label, func.count(MonitorErrorEvent.id), func.count(func.distinct(actor)))
        .where(*scoped)
        .group_by(release_label)
        .order_by(func.count(MonitorErrorEvent.id).desc())
        .limit(20)
    )).all()
    url_label = func.coalesce(MonitorErrorEvent.url, "(none)")
    url_rows = (await db.execute(
        select(url_label, func.count(MonitorErrorEvent.id), func.count(func.distinct(actor)))
        .where(*scoped)
        .group_by(url_label)
        .order_by(func.count(MonitorErrorEvent.id).desc())
        .limit(20)
    )).all()

    sample = (await db.execute(
        select(MonitorErrorEvent)
        .where(MonitorErrorEvent.pipeline_id == pipeline_id, MonitorErrorEvent.group_id == group_id)
        .order_by(MonitorErrorEvent.occurred_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    recent_events = list((await db.execute(
        select(MonitorErrorEvent)
        .where(MonitorErrorEvent.pipeline_id == pipeline_id, MonitorErrorEvent.group_id == group_id)
        .order_by(MonitorErrorEvent.occurred_at.desc())
        .limit(20)
    )).scalars().all())

    sample_payload = None
    if sample:
        sample_payload = {
            "message": sample.message,
            "stack": sample.stack,
            "level": sample.level,
            "handled": sample.handled,
            "url": sample.url,
            "release": sample.release,
            "occurredAt": _iso(sample.occurred_at),
        }

    return {
        "data": {
            "issue": _serialize_issue(
                group,
                affected_users=users,
                affected_sessions=sessions,
                occurrences=occ,
                trend=_issue_trend(recent, prior),
            ),
            "sample": sample_payload,
            "facets": {
                "release": [{"value": value, "count": int(count), "users": int(u)} for value, count, u in release_rows],
                "url": [{"value": value, "count": int(count), "users": int(u)} for value, count, u in url_rows],
            },
            "recentOccurrences": [_serialize_error_event(event) for event in recent_events],
            "windowDays": usage_window,
        }
    }


class IssueStatusBody(BaseModel):
    status: Literal["unresolved", "resolved", "ignored"]


@router.get("/{pipeline_id}/issues/{group_id}/sessions")
async def get_issue_sessions(
    pipeline_id: str,
    group_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """The sessions this issue fired in — the issue → affected sessions → session
    timeline thread (where the error shows up in situ). Aggregated from the
    group's error events by session_id."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    group = (await db.execute(
        select(MonitorErrorGroup).where(
            MonitorErrorGroup.id == group_id,
            MonitorErrorGroup.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Issue not found")

    rows = (await db.execute(
        select(
            MonitorErrorEvent.session_id,
            func.max(MonitorErrorEvent.visitor_id),
            func.max(MonitorErrorEvent.user_ref),
            func.count(MonitorErrorEvent.id),
            func.min(MonitorErrorEvent.occurred_at),
            func.max(MonitorErrorEvent.occurred_at),
        )
        .where(
            MonitorErrorEvent.pipeline_id == pipeline_id,
            MonitorErrorEvent.group_id == group_id,
            MonitorErrorEvent.occurred_at >= since,
            MonitorErrorEvent.session_id != None,
        )
        .group_by(MonitorErrorEvent.session_id)
        .order_by(func.max(MonitorErrorEvent.occurred_at).desc())
        .limit(50)
    )).all()

    sessions = []
    for session_id, visitor_id, user_ref, occurrences, first_seen, last_seen in rows:
        sessions.append({
            "sessionId": session_id,
            "visitorId": visitor_id,
            "userRef": user_ref,
            "identified": user_ref is not None,
            "occurrences": int(occurrences),
            "firstSeenAt": _iso(first_seen),
            "lastSeenAt": _iso(last_seen),
        })

    return {
        "data": {
            "issueId": group_id,
            "windowDays": usage_window,
            "sessions": sessions,
        }
    }


@router.patch("/{pipeline_id}/issues/{group_id}")
async def update_issue_status(
    pipeline_id: str,
    group_id: str,
    body: IssueStatusBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Manually set an issue's status. Resolve to clear it from open issues;
    ingest auto-reopens a resolved issue if it recurs (regression). Ignore mutes
    it — recurrence does not reopen an ignored issue."""
    await _require_launched_product(pipeline_id, db, uid)
    group = (await db.execute(
        select(MonitorErrorGroup).where(
            MonitorErrorGroup.id == group_id,
            MonitorErrorGroup.pipeline_id == pipeline_id,
        )
    )).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Issue not found")
    group.status = body.status
    group.updated_at = _now()
    await db.commit()
    return {"data": {"id": group.id, "status": group.status}}


@router.get("/{pipeline_id}/errors/by-release")
async def get_errors_by_release(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Split-by-release across all errors: did this deploy regress? Per release we
    compute the error rate (errors / sessions) so a traffic difference between
    releases doesn't read as a regression on raw counts. Errors carry `release`
    natively; usage sessions carry it since B.4."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    now = _now()
    usage_window = eff.analytics_usage_window_days
    since = now - timedelta(days=usage_window)
    actor = func.coalesce(MonitorErrorEvent.user_ref, MonitorErrorEvent.visitor_id)
    error_release = func.coalesce(MonitorErrorEvent.release, "(none)")
    usage_release = func.coalesce(MonitorUsageEvent.release, "(none)")

    error_rows = (await db.execute(
        select(
            error_release,
            func.count(MonitorErrorEvent.id),
            func.count(func.distinct(actor)),
            func.count(func.distinct(MonitorErrorEvent.session_id)),
            func.min(MonitorErrorEvent.occurred_at),
            func.max(MonitorErrorEvent.occurred_at),
        )
        .where(MonitorErrorEvent.pipeline_id == pipeline_id, MonitorErrorEvent.occurred_at >= since)
        .group_by(error_release)
    )).all()

    # Sessions per release from usage, the denominator for the rate.
    session_rows = (await db.execute(
        select(usage_release, func.count(func.distinct(MonitorUsageEvent.session_id)))
        .where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= since,
            MonitorUsageEvent.session_id != None,
        )
        .group_by(usage_release)
    )).all()
    sessions_by_release = {release: int(count) for release, count in session_rows}

    releases = []
    for release, errors, users, err_sessions, first_seen, last_seen in error_rows:
        sessions = sessions_by_release.get(release, 0)
        releases.append({
            "release": release,
            "errors": int(errors),
            "affectedUsers": int(users),
            "affectedSessions": int(err_sessions),
            "sessions": sessions,
            "errorsPerSession": round(int(errors) / sessions, 4) if sessions else None,
            "firstSeenAt": _iso(first_seen),
            "lastSeenAt": _iso(last_seen),
        })
    # Most recently active release first — the latest deploy is what you're judging.
    releases.sort(key=lambda r: r["lastSeenAt"] or "", reverse=True)

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": _usage_source_is_connected(source, pipeline_id),
            "windowDays": usage_window,
            "releases": releases,
        }
    }


# Per-source ingest rate limit. Fixed one-minute window kept in process: a
# coarse flood guard so one source can't bury ingest, not a precise distributed
# quota (each worker holds its own window). A batch counts as its event total.
_rate_windows: dict[str, list] = {}


def _check_rate_limit(source_id: str, cost: int = 1) -> None:
    limit = get_settings().ingest_rate_limit_per_minute
    if limit <= 0:
        return
    window = int(_now().timestamp()) // 60
    entry = _rate_windows.get(source_id)
    if entry is None or entry[0] != window:
        # New window. Opportunistically drop stale entries so the map can't grow
        # unbounded across many sources.
        if len(_rate_windows) > 10000:
            _rate_windows.clear()
        entry = [window, 0]
        _rate_windows[source_id] = entry
    entry[1] += cost
    if entry[1] > limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded", headers={"Retry-After": "60"})


# Core Web Vitals thresholds (good upper-bound, poor lower-bound). LCP/INP/FCP/
# TTFB are milliseconds; CLS is unitless. A p75 at/below `good` is good, above
# `poor` is poor, between is needs-improvement.
_VITAL_THRESHOLDS = {
    "LCP": (2500.0, 4000.0),
    "INP": (200.0, 500.0),
    "FCP": (1800.0, 3000.0),
    "TTFB": (800.0, 1800.0),
    "CLS": (0.1, 0.25),
}
_VITAL_ORDER = {"LCP": 0, "INP": 1, "CLS": 2, "FCP": 3, "TTFB": 4}


def _rate_vital(metric: str, value: float | None) -> str | None:
    thresholds = _VITAL_THRESHOLDS.get(metric)
    if not thresholds or value is None:
        return None
    good, poor = thresholds
    if value <= good:
        return "good"
    if value <= poor:
        return "needs-improvement"
    return "poor"


def _record_web_vital(source, *, metric, value, rating, url, navigation_id, visitor_id, session_id, user_ref, release, environment, occurred_at, now, trace_id=None, span_id=None, parent_span_id=None, platform=None):
    """Build a web-vital row (not committed). Rating falls back to the
    threshold-derived value when the client didn't send one. Shared by the
    single /vitals endpoint and /batch."""
    return MonitorWebVital(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        metric=metric,
        value=float(value),
        rating=rating or _rate_vital(metric, value),
        url=url,
        navigation_id=navigation_id,
        visitor_id=visitor_id,
        session_id=session_id,
        user_ref=user_ref,
        release=release,
        environment=environment,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        platform=platform,
        occurred_at=occurred_at or now,
        received_at=now,
    )


def _record_log(source, *, level, message, metadata, url, visitor_id, session_id, user_ref, release, environment, occurred_at, now, trace_id=None, span_id=None, parent_span_id=None, platform=None):
    """Build a log row (not committed). Shared by the single /logs endpoint and
    /batch."""
    return MonitorLog(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        level=level,
        message=str(message)[:4000],
        event_metadata=metadata or {},
        url=url,
        visitor_id=visitor_id,
        session_id=session_id,
        user_ref=user_ref,
        release=release,
        environment=environment,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        platform=platform,
        occurred_at=occurred_at or now,
        received_at=now,
    )


_SPAN_KINDS = ("client", "server", "internal")


def _record_span(source, *, trace_id, span_id, parent_span_id, name, kind, service, feature, platform, capture_mode, status, release, environment, visitor_id, session_id, user_ref, attributes, start_at, duration_ms, now):
    """Build a span row (not committed). Shared by /spans and /batch. kind is
    clamped to the allowed set so a malformed client can't trip the CHECK."""
    return MonitorSpan(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=str(name)[:200],
        kind=kind if kind in _SPAN_KINDS else "client",
        service=service,
        feature=feature,
        platform=platform,
        capture_mode=capture_mode,
        status=status,
        release=release,
        environment=environment,
        visitor_id=visitor_id,
        session_id=session_id,
        user_ref=user_ref,
        attributes=attributes or {},
        start_at=start_at or now,
        duration_ms=duration_ms,
        received_at=now,
    )


def _build_usage_event(source, *, event_type, visitor_id, session_id, user_ref, url, referrer, release, environment, metadata, occurred_at, now, trace_id=None, span_id=None, parent_span_id=None, platform=None, capture_mode=None):
    """Build a usage event row (not added/committed). Shared by the single
    `/events` endpoint and `/batch` so both store identical shapes."""
    return MonitorUsageEvent(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        event_type=event_type,
        visitor_id=visitor_id,
        session_id=session_id,
        user_ref=user_ref,
        url=url,
        referrer=referrer,
        release=release,
        environment=environment,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        platform=platform,
        capture_mode=capture_mode,
        event_metadata=metadata or {},
        occurred_at=occurred_at or now,
        received_at=now,
    )


async def _record_error_event(source, db, *, message, stack, level, handled, url, release, environment, visitor_id, session_id, user_ref, metadata, occurred_at, now, error_type=None, trace_id=None, span_id=None, parent_span_id=None, platform=None, capture_mode=None):
    """Upsert the fingerprint group and add the error event (not committed).
    Shared by the single `/errors` endpoint and `/batch` so grouping, regression
    reopen, and first/last-seen tracking stay identical across both."""
    occurred = occurred_at or now
    fingerprint = _error_fingerprint(message, stack)
    title = (_normalize_error_message(message) or message)[:500]

    group = await _error_group(source.pipeline_id, fingerprint, db)
    if group is None:
        group = MonitorErrorGroup(
            id=str(uuid.uuid4()),
            pipeline_id=source.pipeline_id,
            source_id=source.id,
            fingerprint=fingerprint,
            title=title,
            level=level,
            error_type=error_type,
            status="unresolved",
            event_count=1,
            last_release=release,
            first_seen_at=occurred,
            last_seen_at=occurred,
            created_at=now,
            updated_at=now,
        )
        db.add(group)
    else:
        group.event_count = int(group.event_count or 0) + 1
        if group.last_seen_at is None or occurred > group.last_seen_at:
            group.last_seen_at = occurred
        if group.first_seen_at is None or occurred < group.first_seen_at:
            group.first_seen_at = occurred
        if release:
            group.last_release = release
        if error_type and not group.error_type:
            group.error_type = error_type
        # Regression: a previously resolved issue is happening again → reopen it.
        if group.status == "resolved":
            group.status = "unresolved"
        group.updated_at = now

    event = MonitorErrorEvent(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        group_id=group.id,
        fingerprint=fingerprint,
        message=message[:2000],
        stack=stack,
        level=level,
        handled=handled,
        error_type=error_type,
        url=url,
        release=release,
        environment=environment,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        platform=platform,
        capture_mode=capture_mode,
        visitor_id=visitor_id,
        session_id=session_id,
        user_ref=user_ref,
        event_metadata=metadata or {},
        occurred_at=occurred,
        received_at=now,
    )
    db.add(event)
    return event, group


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
