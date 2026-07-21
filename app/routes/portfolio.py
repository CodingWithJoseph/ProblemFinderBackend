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
            ordered.append((s, 0))ێ���$z{-���jםt. Returns a small summary
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
