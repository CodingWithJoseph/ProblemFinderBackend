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
from app.feature_profile import require_deferred_features
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
    require_owned_product as _require_owned_product,
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


class RevenueSourceBody(BaseModel):
    provider: RevenueProvider = "stripe"


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


def _selected_mrr(source: MonitorRevenueSource | None, engine: str) -> tuple[int | None, int | None]:
    """The (current, previous) MRR for the engine the flag selects. ``invoice``
    reads the new engine's totals; anything else stays on the legacy snapshot."""
    if not source:
        return None, None
    if engine == "invoice":
        return source.invoice_mrr_cents, source.previous_invoice_mrr_cents
    return source.current_mrr_cents, source.previous_mrr_cents


def _serialize_revenue_source(source: MonitorRevenueSource | None, engine: str = "subscription") -> dict | None:
    if not source:
        return None
    current_mrr, _previous_mrr = _selected_mrr(source, engine)
    return {
        "id": source.id,
        "pipelineId": source.pipeline_id,
        "provider": source.provider,
        "status": source.status,
        "accountMode": source.account_mode,
        "providerAccountId": source.provider_account_id,
        "providerAccountLabel": source.provider_account_label,
        # currentMrrCents reflects the selected engine; both raw figures are
        # exposed alongside so the two can be compared during cutover.
        "currentMrrCents": current_mrr,
        "revenueEngine": engine,
        "subscriptionMrrCents": source.current_mrr_cents,
        "invoiceMrrCents": source.invoice_mrr_cents,
        "newCustomers30d": source.new_customers_30d,
        "churnedCustomers30d": source.churned_customers_30d,
        "churnRate30d": source.churn_rate_30d,
        "revenueSnapshot": source.revenue_snapshot or {},
        "invoiceRevenueSnapshot": source.invoice_revenue_snapshot or {},
        "grossMarginPct": source.gross_margin_pct,
        "cacCents": source.cac_cents,
        "profitMarginPct": source.profit_margin_pct,
        "connectedAt": _iso(source.connected_at),
        "lastSyncedAt": _iso(source.last_synced_at),
        "createdAt": _iso(source.created_at),
        "updatedAt": _iso(source.updated_at),
    }


async def _revenue_source(pipeline_id: str, db: AsyncSession, provider: str = "stripe") -> MonitorRevenueSource | None:
    return (await db.execute(
        select(MonitorRevenueSource)
        .where(
            MonitorRevenueSource.pipeline_id == pipeline_id,
            MonitorRevenueSource.provider == provider,
        )
        .order_by(MonitorRevenueSource.created_at.asc())
    )).scalar_one_or_none()


async def _revenue_source_by_oauth_state(state: str, db: AsyncSession) -> MonitorRevenueSource | None:
    return (await db.execute(
        select(MonitorRevenueSource)
        .where(
            MonitorRevenueSource.provider == "stripe",
            MonitorRevenueSource.oauth_state == state,
        )
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


def _stripe_connect_redirect_url() -> str:
    return settings.stripe_connect_redirect_url or f"{settings.app_url.rstrip('/')}/api/portfolio/stripe/callback"


def _stripe_connect_return_url(source: MonitorRevenueSource, status: str) -> str:
    params = urlencode({"pipelineId": source.pipeline_id, "stripe": status})
    return f"{settings.app_url.rstrip('/')}/dashboard/monitor/setup?{params}"


def _stripe_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _stripe_items(response: Any) -> list[Any]:
    if hasattr(response, "auto_paging_iter"):
        return list(response.auto_paging_iter())
    return list(_stripe_value(response, "data", []) or [])


def _normalize_interval_cents(amount: int, interval: str, interval_count: int = 1) -> int:
    """Normalize a per-period amount (cents) to a monthly run-rate. Single source
    of truth for annual/weekly/daily → monthly conversion, shared by the
    subscription-object path and the invoice-line path."""
    interval_count = int(interval_count or 1)
    if interval == "year":
        return round(amount / (12 * interval_count))
    if interval == "week":
        return round(amount * 52 / (12 * interval_count))
    if interval == "day":
        return round(amount * 365 / (12 * interval_count))
    return round(amount / interval_count)


def _subscription_item_mrr_cents(item: Any) -> int:
    price = _stripe_value(item, "price", {}) or {}
    quantity = _stripe_value(item, "quantity", 1) or 1
    unit_amount = _stripe_value(price, "unit_amount", 0) or 0
    recurring = _stripe_value(price, "recurring", {}) or {}
    interval = _stripe_value(recurring, "interval", "month")
    interval_count = _stripe_value(recurring, "interval_count", 1) or 1

    amount = int(unit_amount) * int(quantity)
    return _normalize_interval_cents(amount, interval, interval_count)


def _subscription_mrr_cents(subscription: Any) -> int:
    items = _stripe_value(_stripe_value(subscription, "items", {}) or {}, "data", []) or []
    return sum(_subscription_item_mrr_cents(item) for item in items)


def _stripe_account_kwargs(source: MonitorRevenueSource) -> dict:
    """Credential routing for every Stripe read. A ``first_party`` source reads
    the platform's own account via the module-level ``STRIPE_SECRET_KEY`` (no
    ``stripe_account``); a ``connected`` source scopes the call to its Connect
    account. Spread into each ``stripe.*.list`` call so the engine stays
    credential-agnostic."""
    if source.account_mode == "first_party":
        return {}
    return {"stripe_account": source.provider_account_id}


def _credential_path_label(source: MonitorRevenueSource) -> str:
    """Human-readable description of which account a sync pulled from, for the
    Emit log."""
    if source.account_mode == "first_party":
        return "first_party (platform STRIPE_SECRET_KEY)"
    return f"connected (stripe_account={source.provider_account_id})"


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


def _stripe_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _daily_gross_by_day_currency(charges: list[Any]) -> dict[tuple[date, str], int]:
    """Aggregate Stripe charge objects into ``{(utc_date, currency): gross_cents}``.

    Only succeeded charges count, and the amount is the charge's own ``amount``
    (gross — refunds are *not* netted out, matching "gross revenue"). Charges
    without a usable ``created`` timestamp are skipped.
    """
    totals: dict[tuple[date, str], int] = {}
    for charge in charges:
        if _stripe_value(charge, "status") != "succeeded":
            continue
        created = _stripe_timestamp(_stripe_value(charge, "created"))
        if created is None:
            continue
        amount = int(_stripe_value(charge, "amount", 0) or 0)
        if amount <= 0:
            continue
        day = datetime.fromtimestamp(created, tz=timezone.utc).date()
        currency = str(_stripe_value(charge, "currency", "usd") or "usd").lower()
        key = (day, currency)
        totals[key] = totals.get(key, 0) + amount
    return totals


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


@router.get("/overview-metrics", dependencies=[Depends(require_deferred_features)])
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


@router.get("/{pipeline_id}", dependencies=[Depends(require_deferred_features)])
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


async def require_admin_uid(
    uid: str = Depends(get_uid),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Gate: the caller must be on the admin plan. Returns their uid."""
    sub = await db.scalar(select(Subscription).where(Subscription.uid == uid))
    if not sub or sub.plan != PlanEnum.admin:
        raise HTTPException(status_code=403, detail="Admin plan required")
    return uid


@router.post("/products", dependencies=[Depends(require_deferred_features)])
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


@router.get("/admin/settings", dependencies=[Depends(require_deferred_features)])
async def get_admin_settings(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(require_admin_uid),
):
    return {"data": await serialize_settings(db)}


@router.put("/admin/settings", dependencies=[Depends(require_deferred_features)])
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


@router.post("/{pipeline_id}/revenue-source", dependencies=[Depends(require_deferred_features)])
async def create_revenue_source(
    pipeline_id: str,
    body: RevenueSourceBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    product = await _require_launched_product(pipeline_id, db, uid)
    existing = await _revenue_source(product.id, db, body.provider)
    if existing:
        return {"data": _serialize_revenue_source(existing)}

    now = _now()
    source = MonitorRevenueSource(
        id=str(uuid.uuid4()),
        pipeline_id=product.id,
        user_id=uid,
        provider=body.provider,
        status="not_connected",
        created_at=now,
        updated_at=now,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return {"data": _serialize_revenue_source(source)}


@router.post("/{pipeline_id}/revenue-source/first-party", dependencies=[Depends(require_deferred_features)])
async def create_first_party_revenue_source(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(require_admin_uid),
):
    """Admin-only: attach a first-party (own-key) revenue source to a product.
    No Connect handshake — it reads Immensity's own platform account directly via
    STRIPE_SECRET_KEY (whichever key is configured, so test mode reads the test
    account). Admin-only is a hard requirement: first-party sources expose real
    platform revenue."""
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=400, detail="STRIPE_SECRET_KEY is not configured")

    product = await _require_launched_product(pipeline_id, db, uid)
    now = _now()
    source = await _revenue_source(product.id, db)
    if source and source.account_mode != "first_party":
        raise HTTPException(
            status_code=400,
            detail="This product already has a connected revenue source; remove it before switching to first-party",
        )
    if not source:
        source = MonitorRevenueSource(
            id=str(uuid.uuid4()),
            pipeline_id=product.id,
            user_id=uid,
            provider="stripe",
            created_at=now,
            updated_at=now,
        )
        db.add(source)

    # First-party reads the platform account directly: no provider_account_id,
    # no OAuth state. Mark connected so the scheduled sync picks it up.
    source.account_mode = "first_party"
    source.status = "connected"
    source.provider_account_id = None
    source.provider_account_label = "platform (STRIPE_SECRET_KEY)"
    source.connected_at = source.connected_at or now
    source.oauth_state = None
    source.oauth_state_expires_at = None
    source.updated_at = now
    await db.commit()
    await db.refresh(source)
    return {"data": _serialize_revenue_source(source)}


@router.post("/{pipeline_id}/revenue-source/connect", dependencies=[Depends(require_deferred_features)])
async def create_revenue_connect_url(
    pipeline_id: str,
    body: RevenueSourceBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    if body.provider != "stripe":
        raise HTTPException(status_code=400, detail="Unsupported revenue provider")
    if not settings.stripe_connect_client_id:
        raise HTTPException(status_code=400, detail="Stripe Connect is not configured")

    product = await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _revenue_source(product.id, db, body.provider)
    now = _now()
    if not source:
        source = MonitorRevenueSource(
            id=str(uuid.uuid4()),
            pipeline_id=product.id,
            user_id=uid,
            provider=body.provider,
            status="not_connected",
            created_at=now,
            updated_at=now,
        )
        db.add(source)

    source.oauth_state = secrets.token_urlsafe(32)
    source.oauth_state_expires_at = now + timedelta(minutes=eff.stripe_oauth_state_ttl_minutes)
    source.updated_at = now
    await db.commit()
    await db.refresh(source)

    query = urlencode({
        "response_type": "code",
        "client_id": settings.stripe_connect_client_id,
        "scope": "read_write",
        "state": source.oauth_state,
        "redirect_uri": _stripe_connect_redirect_url(),
    })
    return {"data": {"url": f"https://connect.stripe.com/oauth/authorize?{query}"}}


@router.get("/revenue-source/stripe/callback", dependencies=[Depends(require_deferred_features)])
async def complete_revenue_connect(
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
):
    source = await _revenue_source_by_oauth_state(state, db)
    if not source or not source.oauth_state_expires_at or source.oauth_state_expires_at < _now():
        raise HTTPException(status_code=400, detail="Invalid or expired Stripe connection state")

    token = stripe.OAuth.token(grant_type="authorization_code", code=code)
    account_id = token.get("stripe_user_id") if isinstance(token, dict) else getattr(token, "stripe_user_id", None)
    if not account_id:
        raise HTTPException(status_code=400, detail="Stripe did not return a connected account")

    now = _now()
    source.provider_account_id = account_id
    source.provider_account_label = account_id
    source.status = "connected"
    source.connected_at = now
    source.updated_at = now
    source.oauth_state = None
    source.oauth_state_expires_at = None
    await db.commit()
    await db.refresh(source)
    return {"data": {"redirectUrl": _stripe_connect_return_url(source, "connected")}}


@router.get("/{pipeline_id}/revenue", dependencies=[Depends(require_deferred_features)])
async def get_revenue_metrics(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _revenue_source(pipeline_id, db)
    selected_mrr, _previous = _selected_mrr(source, eff.revenue_engine)
    metrics = await _compute_revenue_metrics(source, db, eff, eff.revenue_sync_window_days) if source else None
    return {
        "data": {
            "source": _serialize_revenue_source(source, eff.revenue_engine),
            "connected": _revenue_source_is_connected(source, pipeline_id),
            "summary": {
                "mrrCents": selected_mrr,
                "subscriptionMrrCents": source.current_mrr_cents if source else None,
                "invoiceMrrCents": source.invoice_mrr_cents if source else None,
                "revenueEngine": eff.revenue_engine,
                "newCustomers30d": source.new_customers_30d if source else None,
                "churnedCustomers30d": source.churned_customers_30d if source else None,
                "churnRate30d": source.churn_rate_30d if source else None,
                "windowDays": eff.revenue_sync_window_days,
            },
            "metrics": metrics,
        }
    }


@router.post("/{pipeline_id}/revenue/sync", dependencies=[Depends(require_deferred_features)])
async def sync_revenue_metrics(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _revenue_source(pipeline_id, db)
    if not _source_is_syncable(source):
        raise HTTPException(status_code=400, detail="Stripe is not connected for this product")

    try:
        await _perform_revenue_sync(source, db, eff.revenue_sync_window_days)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not sync Stripe revenue") from exc

    selected_mrr, _previous = _selected_mrr(source, eff.revenue_engine)
    metrics = await _compute_revenue_metrics(source, db, eff, eff.revenue_sync_window_days)
    return {
        "data": {
            "source": _serialize_revenue_source(source, eff.revenue_engine),
            "connected": True,
            "summary": {
                "mrrCents": selected_mrr,
                "subscriptionMrrCents": source.current_mrr_cents,
                "invoiceMrrCents": source.invoice_mrr_cents,
                "revenueEngine": eff.revenue_engine,
                "newCustomers30d": source.new_customers_30d,
                "churnedCustomers30d": source.churned_customers_30d,
                "churnRate30d": source.churn_rate_30d,
                "windowDays": eff.revenue_sync_window_days,
            },
            "metrics": metrics,
        }
    }


class RevenueEconomicsBody(BaseModel):
    # All optional: only fields present in the request are applied. A null value
    # clears the override and reverts the dependent metric to its default
    # (badged "estimated"). Fractions are 0..1; profit margin may be negative.
    gross_margin_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    cac_cents: int | None = Field(default=None, ge=0)
    profit_margin_pct: float | None = Field(default=None, ge=-1.0, le=1.0)


@router.patch("/{pipeline_id}/revenue/economics", dependencies=[Depends(require_deferred_features)])
async def update_revenue_economics(
    pipeline_id: str,
    body: RevenueEconomicsBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Set the per-source unit-economics inputs (gross margin, CAC, profit
    margin) that LTV / CAC payback / Rule of 40 depend on."""
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _revenue_source(pipeline_id, db)
    if not source:
        raise HTTPException(status_code=404, detail="Revenue source not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(source, field, value)
    source.updated_at = _now()
    await db.commit()
    await db.refresh(source)
    return {"data": _serialize_revenue_source(source, eff.revenue_engine)}


@router.get("/{pipeline_id}/revenue/join-coverage", dependencies=[Depends(require_deferred_features)])
async def get_join_coverage(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Identity join-coverage report: of N identified actors, how many resolved
    via explicit id vs email vs unresolved."""
    await _require_launched_product(pipeline_id, db, uid)
    return {"data": await _join_coverage(db, pipeline_id)}


@router.get("/{pipeline_id}/insights/revenue-correlation", dependencies=[Depends(require_deferred_features)])
async def get_revenue_correlation(
    pipeline_id: str,
    outcomeWindowDays: int = 90,
    observationWindowDays: int | None = None,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Usage↔revenue correlation: which behaviors predict expansion vs churn.
    Labels each joined customer by net MRR movement over the outcome window, then
    scores candidate behaviors with a Laplace-smoothed odds ratio on a 2×2.

    Note: this is the new revenue-correlation insight. The existing usage-vs-
    errors ``/correlation`` endpoint is unrelated and slated for a later rename
    to ``error-correlation``; it is intentionally left untouched here."""
    await _require_launched_product(pipeline_id, db, uid)
    source = await _revenue_source(pipeline_id, db)
    now = _now()
    outcome_start = (now - timedelta(days=outcomeWindowDays)).date()
    observation_days = observationWindowDays or outcomeWindowDays
    observation_since = now - timedelta(days=observation_days)

    # Resolved identities: user_ref → stripe_customer_id.
    customer_by_ref = {
        ref: cid
        for ref, cid in (await db.execute(
            select(MonitorIdentity.user_ref, MonitorIdentity.stripe_customer_id).where(
                MonitorIdentity.pipeline_id == pipeline_id,
                MonitorIdentity.stripe_customer_id != None,
            )
        )).all()
    }

    # Behaviors: event types each joined customer fired in the observation window.
    behaviors_by_customer: dict[str, set] = {}
    for ref, event_type in (await db.execute(
        select(MonitorUsageEvent.user_ref, MonitorUsageEvent.event_type).where(
            MonitorUsageEvent.pipeline_id == pipeline_id,
            MonitorUsageEvent.occurred_at >= observation_since,
            MonitorUsageEvent.user_ref != None,
        )
    )).all():
        customer_id = customer_by_ref.get(ref)
        if customer_id:
            behaviors_by_customer.setdefault(customer_id, set()).add(event_type)

    # Label the cohort: customers active at the window start, positive if their
    # net MRR movement over the window is ≥ 0 (retained/expanded), else negative.
    labeled: dict[str, bool] = {}
    if source:
        customer_rows = [
            (row.stripe_customer_id, row.as_of_date, row.mrr_cents, row.currency)
            for row in (await db.execute(
                select(MonitorCustomerMrr).where(MonitorCustomerMrr.revenue_source_id == source.id)
            )).scalars().all()
        ]
        net_by_customer: dict[str, int] = {}
        for row in (await db.execute(
            select(MonitorMrrMovement).where(MonitorMrrMovement.revenue_source_id == source.id)
        )).scalars().all():
            if outcome_start < row.effective_date <= now.date():
                net_by_customer[row.stripe_customer_id] = net_by_customer.get(row.stripe_customer_id, 0) + row.mrr_delta_cents
        for customer_id in _active_customer_set(customer_rows, outcome_start):
            labeled[customer_id] = net_by_customer.get(customer_id, 0) >= 0

    result = _revenue_correlation(labeled, behaviors_by_customer)
    return {
        "data": {
            "outcomeWindowDays": outcomeWindowDays,
            "observationWindowDays": observation_days,
            "resolvedCustomers": len(set(customer_by_ref.values())),
            "method": "Laplace-smoothed odds ratio on a 2x2 (behavior x net-MRR outcome)",
            **result,
        }
    }


# ── Invoice/event-derived MRR engine (PR1) ────────────────────────────────────
#
# The legacy path below sums active subscriptions: it only sees current state,
# so it can't reconstruct history and misses prorations/discounts/refunds. This
# engine derives MRR *history* and *movements* from invoices + invoice line
# items. It runs alongside the legacy snapshot; the ``revenue_engine`` flag
# selects which total the API surfaces. All the heavy lifting below is pure
# (Stripe objects in, plain rows out) so it's unit-testable without a DB.

MovementType = Literal["new", "expansion", "contraction", "churn", "reactivation"]


def _line_price(line: Any) -> Any:
    # Newer Stripe exposes `price`; older invoice lines carry `plan` directly.
    return _stripe_value(line, "price", None) or (_stripe_value(line, "plan", {}) or {})


def _line_recurring(line: Any) -> Any:
    price = _line_price(line)
    recurring = _stripe_value(price, "recurring", None)
    if recurring:
        return recurring
    # `plan` objects carry interval/interval_count on the object itself.
    if _stripe_value(price, "interval", None):
        return price
    return {}


def _line_parent_details(line: Any) -> Any:
    """The current Stripe API (2024+) moved the subscription association off the
    invoice line itself onto ``line.parent.subscription_item_details``. Returns
    that nested object (or ``{}``)."""
    parent = _stripe_value(line, "parent", {}) or {}
    return _stripe_value(parent, "subscription_item_details", {}) or {}


def _line_subscription_id(line: Any) -> str | None:
    """Subscription id for an invoice line, across API shapes. Checks, in order:
    the deprecated top-level ``line.subscription``, then the current
    ``line.parent.subscription_item_details.subscription``."""
    direct = _stripe_value(line, "subscription")
    if direct:
        return direct
    return _stripe_value(_line_parent_details(line), "subscription") or None


def _line_subscription_item_id(line: Any) -> str | None:
    """Subscription *item* id, top-level (deprecated) or nested under the current
    ``line.parent.subscription_item_details.subscription_item``."""
    direct = _stripe_value(line, "subscription_item")
    if direct:
        return direct
    return _stripe_value(_line_parent_details(line), "subscription_item") or None


def _is_subscription_line(line: Any) -> bool:
    """Recurring-revenue line items only. True when the line is associated to a
    subscription (top-level or via ``parent.subscription_item_details``), is
    typed ``subscription``, or carries recurring price metadata. One-off charges
    and setup fees — no subscription link and no recurring price — are excluded."""
    if _line_subscription_id(line) or _line_subscription_item_id(line):
        return True
    if _stripe_value(line, "type") == "subscription":
        return True
    return bool(_line_recurring(line))


def _invoice_line_mrr_cents(line: Any) -> int:
    """This invoice line's normalized monthly amount, after discounts, in cents.
    Reuses :func:`_normalize_interval_cents` for annual/weekly/daily → monthly so
    normalization lives in exactly one place."""
    price = _line_price(line)
    recurring = _line_recurring(line)
    interval = _stripe_value(recurring, "interval", "month")
    interval_count = _stripe_value(recurring, "interval_count", 1) or 1
    quantity = _stripe_value(line, "quantity", 1) or 1
    unit_amount = _stripe_value(price, "unit_amount", None)
    if unit_amount is not None:
        gross = int(unit_amount) * int(quantity)
    else:
        # Metered/tiered prices expose no unit_amount: fall back to the charged
        # line amount (already the per-period total).
        gross = int(_stripe_value(line, "amount", 0) or 0)
    discounts = sum(
        int(_stripe_value(da, "amount", 0) or 0)
        for da in (_stripe_value(line, "discount_amounts", []) or [])
    )
    return _normalize_interval_cents(gross - discounts, interval, interval_count)


def _line_period_start(line: Any) -> int | None:
    period = _stripe_value(line, "period", {}) or {}
    return _stripe_timestamp(_stripe_value(period, "start"))


def _billing_events_from_invoices(invoices: list[Any], source_id: str, default_currency: str = "usd") -> list[dict]:
    """Flatten invoices into ledger rows, one per recurring subscription line.
    Non-subscription lines (one-off / setup fees) are dropped here so they never
    enter the ledger."""
    events: list[dict] = []
    for invoice in invoices:
        invoice_id = _stripe_value(invoice, "id")
        customer = _stripe_value(invoice, "customer")
        invoice_currency = (_stripe_value(invoice, "currency") or default_currency)
        lines = _stripe_value(_stripe_value(invoice, "lines", {}) or {}, "data", []) or []
        for line in lines:
            if not _is_subscription_line(line):
                continue
            line_id = _stripe_value(line, "id")
            if not line_id:
                continue
            start = _line_period_start(line)
            currency = (_stripe_value(line, "currency") or invoice_currency or default_currency)
            events.append({
                "revenue_source_id": source_id,
                "stripe_customer_id": customer,
                "stripe_subscription_id": _line_subscription_id(line),
                "stripe_invoice_id": invoice_id,
                "stripe_line_item_id": line_id,
                "mrr_cents": _invoice_line_mrr_cents(line),
                "currency": str(currency).lower(),
                "effective_at": datetime.fromtimestamp(start, tz=timezone.utc) if start else None,
                "is_proration": bool(_stripe_value(line, "proration", False)),
            })
    return events


def _customer_mrr_series(
    events: list[dict],
    active_by_customer: dict[str, dict] | None = None,
    as_of: datetime | None = None,
) -> list[tuple]:
    """Per-customer MRR time series from billing events.

    For each customer we track each subscription's latest non-proration monthly
    level; at every invoice period boundary the customer's MRR is the sum of
    those levels. Prorations are skipped so a mid-cycle change counts the new
    plan's full-period line, not the proration delta. A final ``as_of`` point is
    appended from current active subscriptions, which is how a churn (a sub that
    simply stopped invoicing) shows up.

    Returns ``(customer, date, mrr_cents, currency)`` sorted by customer, date.
    """
    active_by_customer = active_by_customer or {}
    as_of_date = (as_of or _now()).date()

    by_customer: dict[str, list] = {}
    for event in events:
        customer = event["stripe_customer_id"]
        if not customer or event["effective_at"] is None or event["is_proration"]:
            continue
        by_customer.setdefault(customer, []).append((
            event["effective_at"].date(),
            event["stripe_subscription_id"] or "_",
            event["mrr_cents"],
            event["currency"],
        ))

    rows_by_key: dict[tuple, tuple] = {}
    for customer in set(by_customer) | set(active_by_customer):
        sub_mrr: dict[str, int] = {}
        currency = "usd"
        # Sorted by date so later same-day entries accumulate before we read the
        # level; the dict keeps the fully-accumulated value per date.
        per_date: dict[date, tuple] = {}
        for (when, sub, mrr, cur) in sorted(by_customer.get(customer, []), key=lambda e: e[0]):
            sub_mrr[sub] = mrr
            currency = cur or currency
            per_date[when] = (sum(sub_mrr.values()), currency)
        for when, (mrr, cur) in per_date.items():
            rows_by_key[(customer, when)] = (customer, when, mrr, cur)
        active = active_by_customer.get(customer)
        has_history = bool(by_customer.get(customer))
        if active is not None:
            # Still subscribed. The MRR amount is the last *invoiced* level —
            # discounts and prorations are already baked into it — so the active
            # subscription's list price never overrides the more-correct invoiced
            # figure. The active sub is used only to confirm the customer is live
            # (and as a fallback amount when there's no invoice yet, e.g. a sub
            # created but not yet billed).
            amount = sum(sub_mrr.values()) if has_history else int(active.get("mrr_cents", 0))
            rows_by_key[(customer, as_of_date)] = (
                customer, as_of_date, amount, active.get("currency") or currency,
            )
        elif has_history:
            # Had billing history but no active subscription now → 0 MRR as of
            # now, which surfaces as a churn when diffed (a no-op if the last
            # level was already 0, e.g. a trial that never converted). The exact
            # cancellation date arrives with the webhook PR; backfill dates it at
            # sync time.
            rows_by_key[(customer, as_of_date)] = (customer, as_of_date, 0, currency)
    return sorted(rows_by_key.values(), key=lambda r: (r[0], r[1]))


def _movements_from_series(rows: list[tuple]) -> list[dict]:
    """Diff consecutive per-customer MRR rows into the five movement types."""
    by_customer: dict[str, list] = {}
    for customer, when, mrr, currency in rows:
        by_customer.setdefault(customer, []).append((when, mrr, currency))

    movements: list[dict] = []
    for customer, series in by_customer.items():
        series.sort(key=lambda r: r[0])
        prior = 0
        seen_nonzero = False
        for (when, mrr, currency) in series:
            delta = mrr - prior
            movement_type: MovementType | None = None
            if delta != 0:
                if prior == 0 and mrr > 0:
                    movement_type = "reactivation" if seen_nonzero else "new"
                elif mrr > prior > 0:
                    movement_type = "expansion"
                elif 0 < mrr < prior:
                    movement_type = "contraction"
                elif mrr == 0 and prior > 0:
                    movement_type = "churn"
            if movement_type:
                movements.append({
                    "stripe_customer_id": customer,
                    "effective_date": when,
                    "movement_type": movement_type,
                    "mrr_delta_cents": delta,
                    "mrr_after_cents": mrr,
                    "currency": currency,
                })
            if mrr > 0:
                seen_nonzero = True
            prior = mrr
    return movements


def _combine_currency_totals(by_currency: dict[str, int], base_currency: str, fx_rates: dict[str, float]) -> tuple[int, list[str]]:
    """Combine per-currency totals into the base currency using static rates.
    Currencies without a rate are excluded and warned about — never silently
    mixed in at a 1:1 rate."""
    base = (base_currency or "usd").lower()
    rates = {str(k).lower(): v for k, v in (fx_rates or {}).items()}
    total = 0
    warnings: list[str] = []
    for currency, amount in by_currency.items():
        currency = str(currency).lower()
        if currency == base:
            total += amount
        elif currency in rates:
            total += round(amount * rates[currency])
        else:
            warnings.append(f"no FX rate for {currency}->{base}; {amount} cents excluded from total")
    return total, warnings


def _total_mrr_at(rows: list[tuple], as_of_date: date, base_currency: str, fx_rates: dict[str, float]) -> tuple[int, dict[str, int], list[str]]:
    """Total MRR at a date: sum each customer's latest row on/before that date,
    grouped by currency, then combined into the base currency."""
    latest: dict[str, tuple] = {}
    for customer, when, mrr, currency in rows:
        if when <= as_of_date:
            existing = latest.get(customer)
            if existing is None or when >= existing[0]:
                latest[customer] = (when, mrr, currency)
    by_currency: dict[str, int] = {}
    for (_when, mrr, currency) in latest.values():
        by_currency[currency] = by_currency.get(currency, 0) + mrr
    total, warnings = _combine_currency_totals(by_currency, base_currency, fx_rates)
    return total, by_currency, warnings


# ── Derived revenue metrics (PR2) ─────────────────────────────────────────────
#
# Churn / NRR / GRR / quick ratio / ARPA / unit economics, computed purely from
# the movement + customer-MRR tables the invoice engine writes. Every ratio is
# returned with its components (starting MRR + summed movement amounts) so a
# wrong figure is traceable to the input that's off without re-deriving by hand.

FIVE_MOVEMENTS = ("new", "expansion", "contraction", "churn", "reactivation")
LTV_CAP_MONTHS = 60  # 5-year lifetime cap; LTV diverges under non-positive churn


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _movement_sums_by_type(movements: list[tuple], base_currency: str, fx_rates: dict[str, float]) -> tuple[dict[str, int], dict[str, int], list[str]]:
    """Sum signed movement deltas per type into the base currency (returned as
    positive magnitudes), plus the distinct customer count per type."""
    by_type_currency: dict[str, dict[str, int]] = {t: {} for t in FIVE_MOVEMENTS}
    customers_by_type: dict[str, set] = {t: set() for t in FIVE_MOVEMENTS}
    for customer, _when, mtype, delta, currency in movements:
        if mtype not in by_type_currency:
            continue
        bucket = by_type_currency[mtype]
        bucket[currency] = bucket.get(currency, 0) + delta
        customers_by_type[mtype].add(customer)
    sums: dict[str, int] = {}
    warnings: list[str] = []
    for mtype, by_currency in by_type_currency.items():
        total, warns = _combine_currency_totals(by_currency, base_currency, fx_rates)
        sums[mtype] = abs(total)  # contraction/churn are negative; report magnitude
        warnings.extend(warns)
    counts = {mtype: len(customers) for mtype, customers in customers_by_type.items()}
    return sums, counts, warnings


def _active_customer_set(customer_rows: list[tuple], as_of_date: date) -> set:
    """Customers whose latest MRR on/before a date is positive."""
    latest: dict[str, tuple] = {}
    for customer, when, mrr, _currency in customer_rows:
        if when <= as_of_date:
            existing = latest.get(customer)
            if existing is None or when >= existing[0]:
                latest[customer] = (when, mrr)
    return {customer for customer, (_w, mrr) in latest.items() if mrr > 0}


def _count_active_customers(customer_rows: list[tuple], as_of_date: date) -> int:
    """Count of customers whose latest MRR on/before a date is positive."""
    return len(_active_customer_set(customer_rows, as_of_date))


def _revenue_metrics(
    customer_rows: list[tuple],
    movements: list[tuple],
    window_start: date,
    as_of: date,
    base_currency: str,
    fx_rates: dict[str, float],
    gross_margin: float | None,
    cac_cents: int | None,
    profit_margin: float | None,
    default_margin: float,
    window_days: int,
) -> dict:
    """All PR2 ratios + unit economics, each shipped with its components."""
    starting_mrr, _sbc, _sw = _total_mrr_at(customer_rows, window_start, base_currency, fx_rates)
    current_mrr, _cbc, _cw = _total_mrr_at(customer_rows, as_of, base_currency, fx_rates)

    window_movements = [m for m in movements if window_start < m[1] <= as_of]
    sums, counts, warnings = _movement_sums_by_type(window_movements, base_currency, fx_rates)
    new_mrr, expansion = sums["new"], sums["expansion"]
    contraction, churn, reactivation = sums["contraction"], sums["churn"], sums["reactivation"]

    customers_at_start = _count_active_customers(customer_rows, window_start)
    active_accounts = _count_active_customers(customer_rows, as_of)
    churned_customers, new_customers = counts["churn"], counts["new"]

    gross_mrr_churn = _safe_ratio(churn + contraction, starting_mrr)
    net_mrr_churn = _safe_ratio(churn + contraction - expansion - reactivation, starting_mrr)
    nrr = _safe_ratio(starting_mrr + expansion - contraction - churn, starting_mrr)
    grr = _safe_ratio(starting_mrr - contraction - churn, starting_mrr)
    logo_churn = _safe_ratio(churned_customers, customers_at_start)
    quick_ratio = _safe_ratio(new_mrr + expansion, contraction + churn)
    arpa_cents = round(current_mrr / active_accounts) if active_accounts else None
    mrr_growth_rate = _safe_ratio(current_mrr - starting_mrr, starting_mrr)

    # ── unit economics (badged "estimated" when an input is defaulted) ──
    margin_estimated = gross_margin is None
    margin = gross_margin if gross_margin is not None else default_margin
    monthly_margin_per_account = round(arpa_cents * margin) if arpa_cents is not None else None

    ltv_cents = None
    if monthly_margin_per_account is not None:
        cap = monthly_margin_per_account * LTV_CAP_MONTHS
        if gross_mrr_churn and gross_mrr_churn > 0:
            ltv_cents = round(min(monthly_margin_per_account / gross_mrr_churn, cap))
        else:
            ltv_cents = round(cap)  # zero/negative churn → formula diverges → cap

    cac_payback_months = None
    ltv_cac = None
    if cac_cents and monthly_margin_per_account:
        cac_payback_months = round(cac_cents / monthly_margin_per_account, 2)
        if ltv_cents is not None:
            ltv_cac = round(ltv_cents / cac_cents, 2)

    rule_of_40 = None
    rule_of_40_estimated = profit_margin is None
    if profit_margin is not None and mrr_growth_rate is not None:
        rule_of_40 = round((mrr_growth_rate + profit_margin) * 100, 1)  # percentage points

    return {
        "windowDays": window_days,
        "components": {
            "startingMrrCents": starting_mrr,
            "currentMrrCents": current_mrr,
            "newMrrCents": new_mrr,
            "expansionMrrCents": expansion,
            "contractionMrrCents": contraction,
            "churnMrrCents": churn,
            "reactivationMrrCents": reactivation,
            "customersAtStart": customers_at_start,
            "activeAccounts": active_accounts,
            "newCustomers": new_customers,
            "churnedCustomers": churned_customers,
            "movementCounts": counts,
        },
        "ratios": {
            "grossMrrChurn": gross_mrr_churn,
            "netMrrChurn": net_mrr_churn,
            "nrr": nrr,
            "grr": grr,
            "logoChurn": logo_churn,
            "quickRatio": quick_ratio,
            "arpaCents": arpa_cents,
            "mrrGrowthRate": mrr_growth_rate,
        },
        "unitEconomics": {
            "grossMarginPct": margin,
            "grossMarginEstimated": margin_estimated,
            "ltvCents": ltv_cents,
            "ltvCapMonths": LTV_CAP_MONTHS,
            "cacCents": cac_cents,
            "cacPaybackMonths": cac_payback_months,
            "ltvCac": ltv_cac,
            "profitMarginPct": profit_margin,
            "ruleOf40": rule_of_40,
            "ruleOf40Estimated": rule_of_40_estimated,
        },
        "warnings": warnings,
    }


async def _compute_revenue_metrics(source: MonitorRevenueSource, db: AsyncSession, eff: Any, window_days: int) -> dict:
    """Load the source's movement + customer-MRR rows and derive the PR2 metrics."""
    customer_rows = [
        (row.stripe_customer_id, row.as_of_date, row.mrr_cents, row.currency)
        for row in (await db.execute(
            select(MonitorCustomerMrr).where(MonitorCustomerMrr.revenue_source_id == source.id)
        )).scalars().all()
    ]
    movements = [
        (row.stripe_customer_id, row.effective_date, row.movement_type, row.mrr_delta_cents, row.currency)
        for row in (await db.execute(
            select(MonitorMrrMovement).where(MonitorMrrMovement.revenue_source_id == source.id)
        )).scalars().all()
    ]
    now = _now()
    base_currency = str(getattr(eff, "revenue_base_currency", "usd") or "usd")
    fx_rates = getattr(eff, "revenue_fx_rates", {}) or {}
    default_margin = float(getattr(eff, "revenue_default_gross_margin_pct", 0.80))
    return _revenue_metrics(
        customer_rows, movements,
        (now - timedelta(days=window_days)).date(), now.date(),
        base_currency, fx_rates,
        source.gross_margin_pct, source.cac_cents, source.profit_margin_pct,
        default_margin, window_days,
    )


# ── Usage↔revenue correlation (D1) ────────────────────────────────────────────
#
# Which behaviors predict expansion vs churn. PostHog method: label each joined
# customer by their net MRR movement over an outcome window, then for each
# candidate behavior build a 2×2 (behavior present/absent × outcome positive/
# negative) and rank by a Laplace-smoothed odds ratio. Every candidate returns
# its raw (a,b,c,d) alongside the OR, and dropped candidates name the guardrail
# that dropped them, so both the math and the sample-size honesty are inspectable.

# Guardrail defaults (practitioner floors).
CORR_MIN_BEHAVIOR_PERSONS = 5      # a+b must clear this
CORR_MIN_BEHAVIOR_PCT = 0.05       # ...and this share of the cohort
CORR_MIN_OUTCOME_PER_CLASS = 3     # need both positives and negatives to compare
CORR_OR_NEUTRAL_BAND = 1.1         # discard OR within ~1.1× of 1.0 (no signal)
CORR_SAMPLE_FLOOR = 50             # caveat below this many labeled outcomes


def _two_by_two(behavior_customers: set, positives: set, negatives: set) -> tuple[int, int, int, int]:
    """(a,b,c,d) = behavior×outcome contingency over the labeled cohort."""
    a = len(behavior_customers & positives)
    b = len(behavior_customers & negatives)
    c = len(positives - behavior_customers)
    d = len(negatives - behavior_customers)
    return a, b, c, d


def _laplace_odds_ratio(a: int, b: int, c: int, d: int) -> float:
    """Laplace-smoothed odds ratio; +1 in every cell avoids divide-by-zero and
    tempers tiny-cell blowups."""
    return ((a + 1) * (d + 1)) / ((c + 1) * (b + 1))


def _fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float | None:
    """Two-sided Fisher's exact p-value for the 2×2, via the hypergeometric
    distribution (no scipy dependency)."""
    from math import comb
    row1, col1, col2 = a + b, a + c, b + d
    n = a + b + c + d
    if n == 0 or row1 == 0 or col1 == 0 or col2 == 0:
        return None

    def hyp(k: int) -> float:
        return comb(col1, k) * comb(col2, row1 - k) / comb(n, row1)

    p_obs = hyp(a)
    total = 0.0
    for k in range(max(0, row1 - col2), min(row1, col1) + 1):
        pk = hyp(k)
        if pk <= p_obs * (1 + 1e-9):
            total += pk
    return round(min(total, 1.0), 6)


def _revenue_correlation(
    labeled: dict[str, bool],
    behaviors_by_customer: dict[str, set],
) -> dict:
    """Score candidate behaviors against the labeled cohort. ``labeled`` maps a
    customer to True (positive: expanded/retained) or False (negative: churned/
    contracted). ``behaviors_by_customer`` maps a customer to the set of event
    types they fired in the observation window."""
    positives = {c for c, ok in labeled.items() if ok}
    negatives = {c for c, ok in labeled.items() if not ok}
    total = len(labeled)
    caveats: list[str] = []
    if total < CORR_SAMPLE_FLOOR:
        caveats.append(
            f"only {total} labeled outcomes (practitioner floor ~{CORR_SAMPLE_FLOOR}); treat results as directional"
        )

    # Heavily-skewed success:failure totals → can't compare; bail with a caveat.
    if min(len(positives), len(negatives)) < CORR_MIN_OUTCOME_PER_CLASS:
        caveats.append(
            f"need ≥{CORR_MIN_OUTCOME_PER_CLASS} customers in each outcome class "
            f"(have {len(positives)} positive / {len(negatives)} negative)"
        )
        return {"candidates": [], "dropped": [], "caveats": caveats,
                "cohort": {"labeled": total, "positive": len(positives), "negative": len(negatives)}}

    candidate_events: set = set()
    for events in behaviors_by_customer.values():
        candidate_events |= events

    kept: list[dict] = []
    dropped: list[dict] = []
    for behavior in sorted(candidate_events):
        behavior_customers = {c for c, events in behaviors_by_customer.items() if behavior in events and c in labeled}
        a, b, c, d = _two_by_two(behavior_customers, positives, negatives)
        odds = _laplace_odds_ratio(a, b, c, d)
        record = {
            "behavior": behavior,
            "a": a, "b": b, "c": c, "d": d,
            "oddsRatio": round(odds, 3),
        }
        present = a + b
        if present < CORR_MIN_BEHAVIOR_PERSONS:
            dropped.append({**record, "reason": f"too few people did it ({present} < {CORR_MIN_BEHAVIOR_PERSONS})"})
            continue
        if total and present / total < CORR_MIN_BEHAVIOR_PCT:
            dropped.append({**record, "reason": f"below {int(CORR_MIN_BEHAVIOR_PCT * 100)}% of cohort"})
            continue
        if (1 / CORR_OR_NEUTRAL_BAND) <= odds <= CORR_OR_NEUTRAL_BAND:
            dropped.append({**record, "reason": f"odds ratio within ~{CORR_OR_NEUTRAL_BAND}× of 1.0 (no signal)"})
            continue
        record["direction"] = "expansion" if odds > 1 else "churn"
        record["expandLikelihood"] = round(odds, 2)
        record["churnLikelihood"] = round(1 / odds, 2)
        record["pValue"] = _fisher_exact_two_sided(a, b, c, d)
        kept.append(record)

    # Rank by distance from 1.0 in both directions.
    import math
    kept.sort(key=lambda r: abs(math.log(r["oddsRatio"])) if r["oddsRatio"] > 0 else 0, reverse=True)
    return {
        "candidates": kept,
        "dropped": dropped,
        "caveats": caveats,
        "cohort": {"labeled": total, "positive": len(positives), "negative": len(negatives)},
    }


async def _persist_billing_events(db: AsyncSession, events: list[dict]) -> int:
    """Append billing events, idempotent on ``stripe_line_item_id`` so re-running
    the backfill never duplicates a row."""
    if not events:
        return 0
    now = _now()
    table = MonitorBillingEvent.__table__
    for event in events:
        await db.execute(
            pg_insert(table)
            .values(id=str(uuid.uuid4()), created_at=now, **event)
            .on_conflict_do_nothing(index_elements=["stripe_line_item_id"])
        )
    await db.flush()
    return len(events)


async def _persist_customer_mrr(db: AsyncSession, source_id: str, rows: list[tuple]) -> None:
    now = _now()
    table = MonitorCustomerMrr.__table__
    for customer, when, mrr, currency in rows:
        await db.execute(
            pg_insert(table)
            .values(
                id=str(uuid.uuid4()), revenue_source_id=source_id, stripe_customer_id=customer,
                as_of_date=when, mrr_cents=mrr, currency=currency, created_at=now, updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["revenue_source_id", "stripe_customer_id", "as_of_date"],
                set_={"mrr_cents": mrr, "currency": currency, "updated_at": now},
            )
        )
    await db.flush()


async def _persist_movements(db: AsyncSession, source_id: str, movements: list[dict]) -> None:
    now = _now()
    table = MonitorMrrMovement.__table__
    for movement in movements:
        await db.execute(
            pg_insert(table)
            .values(id=str(uuid.uuid4()), revenue_source_id=source_id, created_at=now, updated_at=now, **movement)
            .on_conflict_do_update(
                index_elements=["revenue_source_id", "stripe_customer_id", "effective_date"],
                set_={
                    "movement_type": movement["movement_type"],
                    "mrr_delta_cents": movement["mrr_delta_cents"],
                    "mrr_after_cents": movement["mrr_after_cents"],
                    "currency": movement["currency"],
                    "updated_at": now,
                },
            )
        )
    await db.flush()


def _active_subs_by_customer(active_subscriptions: list[Any], base_currency: str) -> dict[str, dict]:
    """Collapse current active subscriptions into per-customer monthly MRR, the
    authoritative 'now' point of the series."""
    by_customer: dict[str, dict] = {}
    for subscription in active_subscriptions:
        customer = _stripe_value(subscription, "customer")
        if not customer:
            continue
        currency = str(_stripe_value(subscription, "currency") or base_currency).lower()
        entry = by_customer.setdefault(customer, {"mrr_cents": 0, "currency": currency})
        entry["mrr_cents"] += _subscription_mrr_cents(subscription)
        entry["currency"] = currency
    return by_customer


async def _run_invoice_engine(source: MonitorRevenueSource, db: AsyncSession, eff: Any) -> dict:
    """Backfill billing events from the connected account's full invoice history,
    rebuild the per-customer MRR series + movements, and store the invoice-engine
    totals on ``source`` alongside the legacy snapshot. Returns a small summary
    for the comparison log."""
    account_kwargs = _stripe_account_kwargs(source)
    base_currency = str(getattr(eff, "revenue_base_currency", "usd") or "usd")
    fx_rates = getattr(eff, "revenue_fx_rates", {}) or {}

    invoices = _stripe_items(stripe.Invoice.list(
        limit=100,
        expand=["data.lines"],
        **account_kwargs,
    ))
    active_subscriptions = _stripe_items(stripe.Subscription.list(
        status="active",
        limit=100,
        **account_kwargs,
    ))

    events = _billing_events_from_invoices(invoices, source.id, base_currency)
    await _persist_billing_events(db, events)

    # Populate the customer directory (id + email) so the identity email-fallback
    # has something to match against, then re-resolve any pending email joins.
    customers = _stripe_items(stripe.Customer.list(limit=100, **account_kwargs))
    await _persist_customers(db, source.id, customers)
    await _resolve_pending_identities(db, source)
    coverage = await _join_coverage(db, source.pipeline_id)
    logger.info(
        "identity join coverage for pipeline %s: explicit=%s email=%s unresolved=%s (resolved rate %s)",
        source.pipeline_id, coverage["explicit"], coverage["email"], coverage["unresolved"], coverage["resolvedRate"],
    )

    now = _now()
    active_by_customer = _active_subs_by_customer(active_subscriptions, base_currency)
    series = _customer_mrr_series(events, active_by_customer, as_of=now)
    await _persist_customer_mrr(db, source.id, series)

    movements = _movements_from_series(series)
    await _persist_movements(db, source.id, movements)

    today = now.date()
    current_total, by_currency, warnings = _total_mrr_at(series, today, base_currency, fx_rates)
    previous_total, _prev_by_currency, _prev_warnings = _total_mrr_at(
        series, (now - timedelta(days=30)).date(), base_currency, fx_rates
    )

    source.previous_invoice_mrr_cents = source.invoice_mrr_cents
    source.invoice_mrr_cents = current_total
    source.invoice_revenue_snapshot = {
        "engine": "invoice",
        "baseCurrency": base_currency.lower(),
        "byCurrency": by_currency,
        "mrrCents": current_total,
        "mrrCents30dAgo": previous_total,
        "billingEvents": len(events),
        "customers": len({row[0] for row in series}),
        "movements": len(movements),
        "warnings": warnings,
        "computedAt": now.isoformat(),
    }
    return {"current_total": current_total, "previous_total": previous_total, "warnings": warnings}


def _log_engine_comparison(source: MonitorRevenueSource, invoice_result: dict) -> None:
    """Validation log: invoice-engine MRR vs the legacy subscription snapshot.
    The invoice figure is the more correct one; deltas come from prorations,
    discounts/refunds, and reconstructed history rather than current state only."""
    subscription_mrr = source.current_mrr_cents or 0
    invoice_mrr = invoice_result["current_total"]
    logger.info(
        "revenue engine comparison for source %s [%s]: subscription=%s cents, invoice=%s cents, delta=%s cents "
        "(invoice engine is more correct; deltas come from prorations/discounts/refunds and reconstructed history)",
        source.id, _credential_path_label(source), subscription_mrr, invoice_mrr, invoice_mrr - subscription_mrr,
    )
    if invoice_result.get("warnings"):
        logger.warning("revenue engine currency warnings for source %s: %s", source.id, invoice_result["warnings"])


# ── Identity join (PR3) ───────────────────────────────────────────────────────
#
# Connects a usage actor (user_ref, set via the tracker's identify()) to a paying
# Stripe customer. Join key is an explicit stripe_customer_id trait; email is the
# fallback, matched against the Stripe customer directory populated during sync.


async def _persist_customers(db: AsyncSession, source_id: str, customers: list[Any]) -> int:
    """Upsert the Stripe customer directory (id + email) for a source."""
    now = _now()
    table = MonitorCustomer.__table__
    written = 0
    for customer in customers:
        customer_id = _stripe_value(customer, "id")
        if not customer_id:
            continue
        email = _stripe_value(customer, "email")
        await db.execute(
            pg_insert(table)
            .values(
                id=str(uuid.uuid4()), revenue_source_id=source_id, stripe_customer_id=customer_id,
                email=email, name=_stripe_value(customer, "name"), created_at=now, updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["revenue_source_id", "stripe_customer_id"],
                set_={"email": email, "name": _stripe_value(customer, "name"), "updated_at": now},
            )
        )
        written += 1
    await db.flush()
    return written


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


async def _resolve_pending_identities(db: AsyncSession, source: MonitorRevenueSource) -> int:
    """Re-resolve identities for this product that aren't explicitly joined yet,
    now that the customer directory is fresh. Returns how many became resolved."""
    email_index = await _customer_email_index(db, source.id)
    pending = (await db.execute(
        select(MonitorIdentity).where(
            MonitorIdentity.pipeline_id == source.pipeline_id,
            MonitorIdentity.resolution_method != "explicit",
        )
    )).scalars().all()
    newly_resolved = 0
    now = _now()
    for identity in pending:
        customer_id, method = _resolve_identity(identity.stripe_customer_id, identity.email, email_index)
        if method != identity.resolution_method or customer_id != identity.stripe_customer_id:
            identity.stripe_customer_id = customer_id
            identity.resolution_method = method
            identity.resolved_at = now if method != "unresolved" else None
            identity.updated_at = now
            if method != "unresolved":
                newly_resolved += 1
    await db.flush()
    return newly_resolved


async def _join_coverage(db: AsyncSession, pipeline_id: str) -> dict:
    """Join-coverage report: of N identified actors, how many resolved via
    explicit id vs email vs unresolved. Validates the layer and predicts how
    strong the correlation inputs will be."""
    rows = (await db.execute(
        select(MonitorIdentity.resolution_method, func.count(MonitorIdentity.id))
        .where(MonitorIdentity.pipeline_id == pipeline_id)
        .group_by(MonitorIdentity.resolution_method)
    )).all()
    counts = {method: int(count) for method, count in rows}
    explicit = counts.get("explicit", 0)
    email = counts.get("email", 0)
    unresolved = counts.get("unresolved", 0)
    total = explicit + email + unresolved
    return {
        "total": total,
        "explicit": explicit,
        "email": email,
        "unresolved": unresolved,
        "resolvedRate": round((explicit + email) / total, 4) if total else None,
    }


async def _sync_daily_revenue(source: MonitorRevenueSource, db: AsyncSession, window_days: int | None = None) -> int:
    """Pull succeeded Stripe charges over the window and upsert one
    ``portfolio_revenue_daily`` row per (day, currency) gross total.

    This is the daily-gross-revenue series behind the Portfolio overview card.
    The upsert recomputes each day's full total from the window's charges, so
    re-running an overlapping window never double-counts. Returns the number of
    day/currency rows written."""
    window = window_days if window_days is not None else settings.revenue_sync_window_days
    since = int((_now() - timedelta(days=window)).timestamp())
    account_kwargs = _stripe_account_kwargs(source)
    charges = _stripe_items(stripe.Charge.list(
        created={"gte": since},
        limit=100,
        **account_kwargs,
    ))
    totals = _daily_gross_by_day_currency(charges)
    now = _now()
    table = MonitorRevenueDaily.__table__
    for (day, currency), gross in sorted(totals.items()):
        await db.execute(
            pg_insert(table)
            .values(
                id=str(uuid.uuid4()), revenue_source_id=source.id, as_of_date=day,
                gross_cents=gross, currency=currency, created_at=now, updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["revenue_source_id", "as_of_date", "currency"],
                set_={"gross_cents": gross, "updated_at": now},
            )
        )
    await db.flush()
    return len(totals)


async def _perform_revenue_sync(source: MonitorRevenueSource, db: AsyncSession, window_days: int | None = None) -> None:
    """Pull fresh metrics from the connected Stripe account onto ``source`` and
    commit. Raises on Stripe failure; callers decide how to surface it."""
    window = window_days if window_days is not None else settings.revenue_sync_window_days
    since = int((_now() - timedelta(days=window)).timestamp())
    account_kwargs = _stripe_account_kwargs(source)
    logger.info("revenue sync for source %s via %s", source.id, _credential_path_label(source))
    active_subscriptions = _stripe_items(stripe.Subscription.list(
        status="active",
        limit=100,
        **account_kwargs,
    ))
    customers = _stripe_items(stripe.Customer.list(
        created={"gte": since},
        limit=100,
        **account_kwargs,
    ))
    canceled_subscriptions = _stripe_items(stripe.Subscription.list(
        status="canceled",
        limit=100,
        **account_kwargs,
    ))

    churned = [
        subscription for subscription in canceled_subscriptions
        if (_stripe_timestamp(_stripe_value(subscription, "canceled_at")) or _stripe_timestamp(_stripe_value(subscription, "ended_at")) or 0) >= since
    ]
    active_count = len(active_subscriptions)
    churned_count = len(churned)
    now = _now()

    # Keep the prior MRR so a drop can be detected for alerts.
    source.previous_mrr_cents = source.current_mrr_cents
    source.current_mrr_cents = sum(_subscription_mrr_cents(subscription) for subscription in active_subscriptions)
    source.new_customers_30d = len(customers)
    source.churned_customers_30d = churned_count
    source.churn_rate_30d = churned_count / (active_count + churned_count) if active_count + churned_count else 0
    source.revenue_snapshot = {
        "provider": "stripe",
        "activeSubscriptions": active_count,
        "newCustomers30d": len(customers),
        "churnedSubscriptions30d": churned_count,
        "syncedAt": now.isoformat(),
    }
    source.last_synced_at = now
    source.updated_at = now

    # New invoice/event engine, computed in parallel. Wrapped in a savepoint and
    # guarded so a failure here can never break the legacy snapshot above: the
    # nested rollback discards only the engine's writes, the outer commit still
    # persists the snapshot.
    try:
        eff = await effective_config(db)
        async with db.begin_nested():
            invoice_result = await _run_invoice_engine(source, db, eff)
        _log_engine_comparison(source, invoice_result)
    except Exception as exc:
        logger.warning("invoice revenue engine failed for source %s: %s", source.id, exc)

    # Daily gross revenue from charges, also savepoint-guarded so a Charge API
    # failure can't roll back the snapshot or the invoice engine above.
    try:
        async with db.begin_nested():
            written = await _sync_daily_revenue(source, db, window_days)
        logger.info("daily revenue sync for source %s wrote %s day(s)", source.id, written)
    except Exception as exc:
        logger.warning("daily revenue sync failed for source %s: %s", source.id, exc)

    await db.commit()
    await db.refresh(source)


async def _sync_connected_revenue_sources(db: AsyncSession) -> int:
    """Re-sync every connected Stripe revenue source. Per-source failures are
    logged and skipped so one bad account can't stall the rest. Returns the
    number successfully synced."""
    eff = await effective_config(db)
    # Connected sources need a provider account id; first-party sources read the
    # platform account directly and have none, so they're included unconditionally.
    sources = list((await db.execute(
        select(MonitorRevenueSource).where(
            MonitorRevenueSource.status == "connected",
            (
                (MonitorRevenueSource.account_mode == "first_party")
                | (MonitorRevenueSource.provider_account_id != None)
            ),
        )
    )).scalars().all())

    synced = 0
    for source in sources:
        try:
            await _perform_revenue_sync(source, db, eff.revenue_sync_window_days)
            synced += 1
        except Exception as exc:
            logger.warning("scheduled revenue sync failed for source %s: %s", source.id, exc)
    return synced


async def run_scheduled_revenue_sync() -> int:
    """Entrypoint for the scheduler: open a session and sync all connected sources."""
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        count = await _sync_connected_revenue_sources(db)
    logger.info("scheduled revenue sync complete: %s source(s)", count)
    return count


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
