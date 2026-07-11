import logging
import secrets
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.auth import get_uid
from app.config import get_settings
from app.models import (
    MonitorInvestigation,
    MonitorInvestigationEntry,
    MonitorProblem,
    MonitorReport,
    Pipeline,
    PlanEnum,
    MonitorAlertSettings,
    MonitorBillingEvent,
    MonitorCustomer,
    MonitorCustomerMrr,
    MonitorErrorEvent,
    MonitorErrorGroup,
    MonitorIdentity,
    MonitorMrrMovement,
    MonitorRevenueDaily,
    MonitorRevenueSource,
    MonitorLog,
    MonitorSpan,
    MonitorUsageEvent,
    MonitorUsageSource,
    MonitorWebVital,
    Subscription,
)
from app.services.alerts import AlertPrefs, alert_settings_row
from app.services.app_settings import apply_settings, effective_config, serialize_settings
from app.services.monitoring.ingest import _error_fingerprint, _normalize_error_message
from app.routes.pipeline import (
    _fetch_issue_counts_for_cards,
    _fetch_posts_for_cards,
    _fetch_teams_for_cards,
    _serialize_cards,
)
from app.routes.portfolio_common import (
    require_launched_product as _require_launched_product,
    require_admin_uid,
    now_utc as _now,
    iso_or_none as _iso,
)
from app.services.revenue import (
    _as_date,
    _combine_currency_totals,
    _pct_change,
    _revenue_source,
    _serialize_revenue_source,
)

import stripe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])
settings = get_settings()
stripe.api_key = settings.stripe_secret_key

UsageEventType = Literal["pageview", "signup", "login", "activation", "custom"]
RevenueProvider = Literal["stripe"]
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


class UsageSourceUpdateBody(BaseModel):
    product_url: str | None = Field(default=None, max_length=2048)
    allowed_domain: str | None = Field(default=None, max_length=255)




class MonitoredProductBody(BaseModel):
    # Admin direct-add: register a product to monitor by hand (name + URL),
    # skipping the discovery pipeline entirely.
    name: str = Field(min_length=1, max_length=200)
    product_url: str = Field(min_length=1, max_length=2048)


class AdminSettingsBody(BaseModel):
    # Map of setting key -> new value. Validated against the whitelist in
    # app/services/app_settings.py, so the UI only sends toggles/dropdown picks.
    updates: dict[str, Any] = Field(default_factory=dict)


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


PORTFOLIO_COMPARISON_DAYS = 7
PORTFOLIO_SPARKLINE_DAYS = 14


def _portfolio_metric_payload(
    metric: str,
    dates: list[date],
    values: list[int | None],
    *,
    unit: str,
    positive_when_up: bool,
    display_total: int | None,
    comparison_current: int | None,
    comparison_prior: int | None,
    comparison_date: date | None = None,
) -> dict:
    """Build one card payload while keeping headline and trend semantics separate."""
    if comparison_current is None or comparison_prior is None:
        direction = "flat"
        is_positive = None
        percent_change = None
    else:
        direction = "up" if comparison_current > comparison_prior else "down" if comparison_current < comparison_prior else "flat"
        is_positive = None if direction == "flat" else (direction == "up") == positive_when_up
        percent_change = _pct_change(comparison_current, comparison_prior)

    return {
        "metric": metric,
        "unit": unit,
        "currentTotal": display_total,
        "priorTotal": comparison_prior,
        "comparisonCurrent": comparison_current,
        "percentChange": percent_change,
        "trendDirection": direction,
        "isPositiveTrend": is_positive,
        "comparisonDate": comparison_date.isoformat() if comparison_date else None,
        "points": [
            {"date": day.isoformat(), "value": value}
            for day, value in zip(dates, values)
        ],
    }


def _previous_month_same_day(day: date) -> date:
    month = 12 if day.month == 1 else day.month - 1
    year = day.year - 1 if day.month == 1 else day.year
    return date(year, month, min(day.day, monthrange(year, month)[1]))




def _portfolio_daily_revenue_values(
    rows: list[MonitorRevenueDaily],
    dates: list[date],
    base_currency: str,
    fx_rates: dict[str, float],
) -> list[int]:
    """Daily gross revenue per date in base-currency cents, zero-filled.

    Days with no charge row read as 0 so the sparkline is a continuous daily
    line (and the card can compare trailing 7 days to the prior 7).
    """
    by_day: dict[date, dict[str, int]] = {}
    for row in rows:
        bucket = by_day.setdefault(row.as_of_date, {})
        bucket[row.currency] = bucket.get(row.currency, 0) + int(row.gross_cents or 0)
    values: list[int] = []
    for day in dates:
        totals = by_day.get(day)
        if not totals:
            values.append(0)
        else:
            values.append(_combine_currency_totals(totals, base_currency, fx_rates)[0])
    return values


@router.get("/overview-metrics")
async def get_portfolio_overview_metrics(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Portfolio overview totals with 7d comparisons and 14 daily points.

    This is deliberately not a configurable analytics endpoint. The overview
    has one stable comparison so cards remain comparable and cacheable.
    """
    pipeline_rows = (await db.execute(
        select(Pipeline.id).where(Pipeline.user_id == uid, Pipeline.launched_at != None)
    )).all()
    pipeline_ids = [row[0] for row in pipeline_rows]

    today = _now().date()
    dates = [today - timedelta(days=offset) for offset in range(PORTFOLIO_SPARKLINE_DAYS - 1, -1, -1)]
    since = datetime.combine(dates[0], datetime.min.time(), tzinfo=timezone.utc)
    usage_by_day = {day: {"traffic": 0, "usage": 0} for day in dates}
    errors_by_day = {day: 0 for day in dates}
    revenue_values: list[int | None] = [None for _ in dates]
    all_time_traffic = 0
    all_time_usage = 0
    unresolved_errors = 0

    if pipeline_ids:
        all_time_usage_rows = (await db.execute(
            select(MonitorUsageEvent.event_type, func.count(MonitorUsageEvent.id))
            .where(MonitorUsageEvent.pipeline_id.in_(pipeline_ids))
            .group_by(MonitorUsageEvent.event_type)
        )).all()
        for event_type, count in all_time_usage_rows:
            count = int(count or 0)
            all_time_usage += count
            if event_type == "pageview":
                all_time_traffic += count

        usage_rows = (await db.execute(
            select(
                func.date(MonitorUsageEvent.occurred_at),
                MonitorUsageEvent.event_type,
                func.count(MonitorUsageEvent.id),
            )
            .where(
                MonitorUsageEvent.pipeline_id.in_(pipeline_ids),
                MonitorUsageEvent.occurred_at >= since,
            )
            .group_by(func.date(MonitorUsageEvent.occurred_at), MonitorUsageEvent.event_type)
        )).all()
        for day_value, event_type, count in usage_rows:
            day = _as_date(day_value)
            if day not in usage_by_day:
                continue
            usage_by_day[day]["usage"] += int(count or 0)
            if event_type == "pageview":
                usage_by_day[day]["traffic"] += int(count or 0)

        error_rows = (await db.execute(
            select(func.date(MonitorErrorEvent.occurred_at), func.count(MonitorErrorEvent.id))
            .where(
                MonitorErrorEvent.pipeline_id.in_(pipeline_ids),
                MonitorErrorEvent.occurred_at >= since,
            )
            .group_by(func.date(MonitorErrorEvent.occurred_at))
        )).all()
        for day_value, count in error_rows:
            day = _as_date(day_value)
            if day in errors_by_day:
                errors_by_day[day] = int(count or 0)

        unresolved_errors = int((await db.execute(
            select(func.count(MonitorErrorGroup.id)).where(
                MonitorErrorGroup.pipeline_id.in_(pipeline_ids),
                MonitorErrorGroup.status == "unresolved",
            )
        )).scalar() or 0)

        eff = await effective_config(db)
        revenue_sources = list((await db.execute(
            select(MonitorRevenueSource).where(MonitorRevenueSource.pipeline_id.in_(pipeline_ids))
        )).scalars().all())
        source_ids = [source.id for source in revenue_sources]
        if source_ids:
            # Daily gross revenue (Stripe charges), zero-filled per day so the
            # card reads like traffic/usage/errors: a continuous daily line with
            # a trailing-7-days vs prior-7-days comparison. Once a source exists
            # every day has a value (0 when no charges settled), which is why the
            # series switches from None-filled to numbers here.
            revenue_daily_rows = list((await db.execute(
                select(MonitorRevenueDaily).where(
                    MonitorRevenueDaily.revenue_source_id.in_(source_ids),
                    MonitorRevenueDaily.as_of_date >= dates[0],
                    MonitorRevenueDaily.as_of_date <= today,
                )
            )).scalars().all())
            revenue_values = _portfolio_daily_revenue_values(
                revenue_daily_rows,
                dates,
                str(eff.revenue_base_currency or "usd"),
                eff.revenue_fx_rates or {},
            )

    traffic_values = [usage_by_day[day]["traffic"] for day in dates]
    usage_values = [usage_by_day[day]["usage"] for day in dates]
    error_values = [errors_by_day[day] for day in dates]
    traffic_current = sum(traffic_values[-PORTFOLIO_COMPARISON_DAYS:])
    traffic_prior = sum(traffic_values[:PORTFOLIO_COMPARISON_DAYS])
    usage_current = sum(usage_values[-PORTFOLIO_COMPARISON_DAYS:])
    usage_prior = sum(usage_values[:PORTFOLIO_COMPARISON_DAYS])
    errors_current = sum(error_values[-PORTFOLIO_COMPARISON_DAYS:])
    errors_prior = sum(error_values[:PORTFOLIO_COMPARISON_DAYS])
    # Revenue trend mirrors the other cards: trailing 7 days vs the prior 7. When
    # no source is connected the series is all-None, so both totals stay None and
    # the card reads as "No data" (the frontend still draws a flat baseline line).
    revenue_has_data = any(value is not None for value in revenue_values)
    revenue_numeric = [value or 0 for value in revenue_values]
    revenue_current = sum(revenue_numeric[-PORTFOLIO_COMPARISON_DAYS:]) if revenue_has_data else None
    revenue_prior = sum(revenue_numeric[:PORTFOLIO_COMPARISON_DAYS]) if revenue_has_data else None
    metrics = [
        _portfolio_metric_payload("traffic", dates, traffic_values, unit="count", positive_when_up=True, display_total=all_time_traffic, comparison_current=traffic_current, comparison_prior=traffic_prior),
        _portfolio_metric_payload("usage", dates, usage_values, unit="count", positive_when_up=True, display_total=all_time_usage, comparison_current=usage_current, comparison_prior=usage_prior),
        _portfolio_metric_payload("revenue", dates, revenue_values, unit="cents", positive_when_up=True, display_total=revenue_current, comparison_current=revenue_current, comparison_prior=revenue_prior),
        _portfolio_metric_payload("errors", dates, error_values, unit="count", positive_when_up=False, display_total=unresolved_errors, comparison_current=errors_current, comparison_prior=errors_prior),
    ]
    return {
        "data": {
            "comparisonDays": PORTFOLIO_COMPARISON_DAYS,
            "sparklineDays": PORTFOLIO_SPARKLINE_DAYS,
            "metrics": metrics,
        }
    }


@router.get("")
async def get_portfolio(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.launched_at != None,
        ).order_by(Pipeline.launched_at.desc())
    )
    cards = result.scalars().all()
    posts_map = await _fetch_posts_for_cards(list(cards), db)
    teams_map = await _fetch_teams_for_cards(list(cards), db)
    counts_map = await _fetch_issue_counts_for_cards(list(cards), db)
    return {"data": _serialize_cards(list(cards), posts_map, teams_map, counts_map)}


@router.get("/{pipeline_id}")
async def get_portfolio_product(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    product = await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    posts_map = await _fetch_posts_for_cards([product], db)
    teams_map = await _fetch_teams_for_cards([product], db)
    counts_map = await _fetch_issue_counts_for_cards([product], db)
    return {
        "data": {
            **_serialize_cards([product], posts_map, teams_map, counts_map)[0],
            "usageSource": _serialize_usage_source(await _usage_source(pipeline_id, db)),
            "revenueSource": _serialize_revenue_source(await _revenue_source(pipeline_id, db), eff.revenue_engine),
        }
    }




@router.post("/products")
async def create_monitored_product(
    body: MonitoredProductBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(require_admin_uid),
):
    """Admin direct-add: create a launched product plus its usage source so
    monitoring is live immediately, without going through discovery."""
    now = _now()
    name = _clean_text(body.name) or "Untitled product"
    url = _clean_text(body.product_url)

    product = Pipeline(
        id=str(uuid.uuid4()),
        user_id=uid,
        name=name,
        url=url,
        launched_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(product)

    source = MonitorUsageSource(
        id=str(uuid.uuid4()),
        pipeline_id=product.id,
        user_id=uid,
        public_key=secrets.token_urlsafe(24),
        status="connected",
        product_url=url,
        allowed_domain=_domain_from_url(url),
        created_at=now,
        updated_at=now,
    )
    db.add(source)

    await db.commit()
    await db.refresh(product)
    await db.refresh(source)

    posts_map = await _fetch_posts_for_cards([product], db)
    teams_map = await _fetch_teams_for_cards([product], db)
    counts_map = await _fetch_issue_counts_for_cards([product], db)
    return {
        "data": {
            **_serialize_cards([product], posts_map, teams_map, counts_map)[0],
            "usageSource": _serialize_usage_source(source),
            "revenueSource": None,
        }
    }


@router.get("/admin/settings")
async def get_admin_settings(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(require_admin_uid),
):
    return {"data": await serialize_settings(db)}


@router.put("/admin/settings")
async def update_admin_settings(
    body: AdminSettingsBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(require_admin_uid),
):
    try:
        await apply_settings(db, body.updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"data": await serialize_settings(db)}






















# ── Invoice/event-derived MRR engine (PR1) ────────────────────────────────────
#
# The legacy path below sums active subscriptions: it only sees current state,
# so it can't reconstruct history and misses prorations/discounts/refunds. This
# engine derives MRR *history* and *movements* from invoices + invoice line
# items. It runs alongside the legacy snapshot; the ``revenue_engine`` flag
# selects which total the API surfaces. All the heavy lifting below is pure
# (Stripe objects in, plain rows out) so it's unit-testable without a DB.





























# ── Derived revenue metrics (PR2) ─────────────────────────────────────────────
#
# Churn / NRR / GRR / quick ratio / ARPA / unit economics, computed purely from
# the movement + customer-MRR tables the invoice engine writes. Every ratio is
# returned with its components (starting MRR + summed movement amounts) so a
# wrong figure is traceable to the input that's off without re-deriving by hand.















# ── Usage↔revenue correlation (D1) ────────────────────────────────────────────
#
# Which behaviors predict expansion vs churn. PostHog method: label each joined
# customer by their net MRR movement over an outcome window, then for each
# candidate behavior build a 2×2 (behavior present/absent × outcome positive/
# negative) and rank by a Laplace-smoothed odds ratio. Every candidate returns
# its raw (a,b,c,d) alongside the OR, and dropped candidates name the guardrail
# that dropped them, so both the math and the sample-size honesty are inspectable.

# Guardrail defaults (practitioner floors).






















# ── Identity join (PR3) ───────────────────────────────────────────────────────
#
# Connects a usage actor (user_ref, set via the tracker's identify()) to a paying
# Stripe customer. Join key is an explicit stripe_customer_id trait; email is the
# fallback, matched against the Stripe customer directory populated during sync.






















