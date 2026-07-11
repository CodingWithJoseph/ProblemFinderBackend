"""Revenue + Stripe-connect routes for a monitored product.

Extracted from routes/portfolio.py to keep that module focused. Handlers delegate
to app.services.revenue; shared guards come from routes/portfolio_common. Mounted
under the /portfolio prefix (all paths are >=2 segments, so they never collide with
the portfolio router's /{pipeline_id} catch-all — include order is irrelevant).
"""

import secrets
import uuid
from datetime import timedelta
from typing import Literal
from urllib.parse import urlencode

import stripe
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.config import get_settings
from app.db import get_db
from app.models import (
    MonitorCustomerMrr,
    MonitorIdentity,
    MonitorMrrMovement,
    MonitorRevenueSource,
    MonitorUsageEvent,
)
from app.services.app_settings import effective_config
from app.routes.portfolio_common import (
    require_launched_product as _require_launched_product,
    require_admin_uid,
    now_utc as _now,
)
from app.services.revenue import (
    _revenue_source,
    _serialize_revenue_source,
    _revenue_source_by_oauth_state,
    _source_is_syncable,
    _compute_revenue_metrics,
    _perform_revenue_sync,
    _join_coverage,
    _revenue_correlation,
    _revenue_source_is_connected,
    _selected_mrr,
    _active_customer_set,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio-revenue"])
settings = get_settings()

RevenueProvider = Literal["stripe"]


class RevenueSourceBody(BaseModel):
    provider: RevenueProvider = "stripe"


def _stripe_connect_redirect_url() -> str:
    return settings.stripe_connect_redirect_url or f"{settings.app_url.rstrip('/')}/api/portfolio/stripe/callback"


def _stripe_connect_return_url(source: MonitorRevenueSource, status: str) -> str:
    params = urlencode({"pipelineId": source.pipeline_id, "stripe": status})
    return f"{settings.app_url.rstrip('/')}/dashboard/monitor/setup?{params}"


@router.post("/{pipeline_id}/revenue-source")
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


@router.post("/{pipeline_id}/revenue-source/first-party")
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


@router.post("/{pipeline_id}/revenue-source/connect")
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


@router.get("/revenue-source/stripe/callback")
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


@router.get("/{pipeline_id}/revenue")
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


@router.post("/{pipeline_id}/revenue/sync")
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


@router.patch("/{pipeline_id}/revenue/economics")
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


@router.get("/{pipeline_id}/revenue/join-coverage")
async def get_join_coverage(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Identity join-coverage report: of N identified actors, how many resolved
    via explicit id vs email vs unresolved."""
    await _require_launched_product(pipeline_id, db, uid)
    return {"data": await _join_coverage(db, pipeline_id)}


@router.get("/{pipeline_id}/insights/revenue-correlation")
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
