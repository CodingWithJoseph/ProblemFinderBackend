import hashlib
import logging
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.auth import get_uid
from app.config import get_settings
from app.models import (
    Pipeline,
    PlanEnum,
    PortfolioAlertSettings,
    PortfolioErrorEvent,
    PortfolioErrorGroup,
    PortfolioRevenueSource,
    PortfolioUsageEvent,
    PortfolioUsageSource,
    Subscription,
)
from app.services.alerts import AlertPrefs, alert_settings_row
from app.services.app_settings import apply_settings, effective_config, serialize_settings
from app.routes.pipeline import (
    _fetch_issue_counts_for_cards,
    _fetch_posts_for_cards,
    _fetch_teams_for_cards,
    _serialize_cards,
)
import stripe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])
public_router = APIRouter(prefix="/public/portfolio", tags=["public-portfolio"])
settings = get_settings()
stripe.api_key = settings.stripe_secret_key

UsageEventType = Literal["pageview", "signup", "login", "activation", "custom"]
RevenueProvider = Literal["stripe"]
ErrorLevel = Literal["error", "warning"]


class UsageEventBody(BaseModel):
    product_id: str
    key: str
    event_type: UsageEventType
    visitor_id: str | None = None
    session_id: str | None = None
    user_id: str | None = Field(default=None, max_length=256)
    url: str | None = None
    referrer: str | None = None
    metadata: dict | None = None
    occurred_at: datetime | None = None


class ErrorEventBody(BaseModel):
    product_id: str
    key: str
    message: str = Field(min_length=1, max_length=2000)
    stack: str | None = Field(default=None, max_length=20000)
    level: ErrorLevel = "error"
    handled: bool | None = None
    url: str | None = None
    release: str | None = Field(default=None, max_length=128)
    visitor_id: str | None = None
    session_id: str | None = None
    user_id: str | None = Field(default=None, max_length=256)
    metadata: dict | None = None
    occurred_at: datetime | None = None


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


def _serialize_usage_source(source: PortfolioUsageSource | None) -> dict | None:
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


def _serialize_usage_event(event: PortfolioUsageEvent) -> dict:
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
        "metadata": event.event_metadata or {},
        "occurredAt": _iso(event.occurred_at),
        "receivedAt": _iso(event.received_at),
    }


def _serialize_error_group(group: PortfolioErrorGroup, affected_sessions: int | None = None) -> dict:
    payload = {
        "id": group.id,
        "pipelineId": group.pipeline_id,
        "fingerprint": group.fingerprint,
        "title": group.title,
        "level": group.level,
        "status": group.status,
        "eventCount": int(group.event_count or 0),
        "lastRelease": group.last_release,
        "firstSeenAt": _iso(group.first_seen_at),
        "lastSeenAt": _iso(group.last_seen_at),
    }
    if affected_sessions is not None:
        payload["affectedSessions"] = int(affected_sessions)
    return payload


def _serialize_error_event(event: PortfolioErrorEvent) -> dict:
    return {
        "id": event.id,
        "pipelineId": event.pipeline_id,
        "groupId": event.group_id,
        "fingerprint": event.fingerprint,
        "message": event.message,
        "stack": event.stack,
        "level": event.level,
        "handled": event.handled,
        "url": event.url,
        "release": event.release,
        "visitorId": event.visitor_id,
        "sessionId": event.session_id,
        "userId": event.user_ref,
        "metadata": event.event_metadata or {},
        "occurredAt": _iso(event.occurred_at),
        "receivedAt": _iso(event.received_at),
    }


def _serialize_revenue_source(source: PortfolioRevenueSource | None) -> dict | None:
    if not source:
        return None
    return {
        "id": source.id,
        "pipelineId": source.pipeline_id,
        "provider": source.provider,
        "status": source.status,
        "providerAccountId": source.provider_account_id,
        "providerAccountLabel": source.provider_account_label,
        "currentMrrCents": source.current_mrr_cents,
        "newCustomers30d": source.new_customers_30d,
        "churnedCustomers30d": source.churned_customers_30d,
        "churnRate30d": source.churn_rate_30d,
        "revenueSnapshot": source.revenue_snapshot or {},
        "connectedAt": _iso(source.connected_at),
        "lastSyncedAt": _iso(source.last_synced_at),
        "createdAt": _iso(source.created_at),
        "updatedAt": _iso(source.updated_at),
    }


async def _revenue_source(pipeline_id: str, db: AsyncSession, provider: str = "stripe") -> PortfolioRevenueSource | None:
    return (await db.execute(
        select(PortfolioRevenueSource)
        .where(
            PortfolioRevenueSource.pipeline_id == pipeline_id,
            PortfolioRevenueSource.provider == provider,
        )
        .order_by(PortfolioRevenueSource.created_at.asc())
    )).scalar_one_or_none()


async def _revenue_source_by_oauth_state(state: str, db: AsyncSession) -> PortfolioRevenueSource | None:
    return (await db.execute(
        select(PortfolioRevenueSource)
        .where(
            PortfolioRevenueSource.provider == "stripe",
            PortfolioRevenueSource.oauth_state == state,
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


def _usage_event_domain_allowed(source: PortfolioUsageSource, request: Request, body: UsageEventBody) -> bool:
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


async def _error_group(pipeline_id: str, fingerprint: str, db: AsyncSession) -> PortfolioErrorGroup | None:
    return (await db.execute(
        select(PortfolioErrorGroup).where(
            PortfolioErrorGroup.pipeline_id == pipeline_id,
            PortfolioErrorGroup.fingerprint == fingerprint,
        )
    )).scalar_one_or_none()


def _stripe_connect_redirect_url() -> str:
    return settings.stripe_connect_redirect_url or f"{settings.app_url.rstrip('/')}/api/portfolio/stripe/callback"


def _stripe_connect_return_url(source: PortfolioRevenueSource, status: str) -> str:
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


def _subscription_item_mrr_cents(item: Any) -> int:
    price = _stripe_value(item, "price", {}) or {}
    quantity = _stripe_value(item, "quantity", 1) or 1
    unit_amount = _stripe_value(price, "unit_amount", 0) or 0
    recurring = _stripe_value(price, "recurring", {}) or {}
    interval = _stripe_value(recurring, "interval", "month")
    interval_count = _stripe_value(recurring, "interval_count", 1) or 1

    amount = int(unit_amount) * int(quantity)
    if interval == "year":
        return round(amount / (12 * interval_count))
    if interval == "week":
        return round(amount * 52 / (12 * interval_count))
    if interval == "day":
        return round(amount * 365 / (12 * interval_count))
    return round(amount / interval_count)


def _subscription_mrr_cents(subscription: Any) -> int:
    items = _stripe_value(_stripe_value(subscription, "items", {}) or {}, "data", []) or []
    return sum(_subscription_item_mrr_cents(item) for item in items)


def _stripe_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


async def _usage_source(pipeline_id: str, db: AsyncSession) -> PortfolioUsageSource | None:
    return (await db.execute(
        select(PortfolioUsageSource)
        .where(PortfolioUsageSource.pipeline_id == pipeline_id)
        .order_by(PortfolioUsageSource.created_at.asc())
    )).scalar_one_or_none()


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
    posts_map = await _fetch_posts_for_cards([product], db)
    teams_map = await _fetch_teams_for_cards([product], db)
    counts_map = await _fetch_issue_counts_for_cards([product], db)
    return {
        "data": {
            **_serialize_cards([product], posts_map, teams_map, counts_map)[0],
            "usageSource": _serialize_usage_source(await _usage_source(pipeline_id, db)),
            "revenueSource": _serialize_revenue_source(await _revenue_source(pipeline_id, db)),
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

    source = PortfolioUsageSource(
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
    source = PortfolioUsageSource(
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
    source = PortfolioRevenueSource(
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
        source = PortfolioRevenueSource(
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
    return {
        "data": {
            "source": _serialize_revenue_source(source),
            "connected": bool(source and source.status == "connected"),
            "summary": {
                "mrrCents": source.current_mrr_cents if source else None,
                "newCustomers30d": source.new_customers_30d if source else None,
                "churnedCustomers30d": source.churned_customers_30d if source else None,
                "churnRate30d": source.churn_rate_30d if source else None,
                "windowDays": eff.revenue_sync_window_days,
            },
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
    if not source or source.status != "connected" or not source.provider_account_id:
        raise HTTPException(status_code=400, detail="Stripe is not connected for this product")

    try:
        await _perform_revenue_sync(source, db, eff.revenue_sync_window_days)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not sync Stripe revenue") from exc

    return {
        "data": {
            "source": _serialize_revenue_source(source),
            "connected": True,
            "summary": {
                "mrrCents": source.current_mrr_cents,
                "newCustomers30d": source.new_customers_30d,
                "churnedCustomers30d": source.churned_customers_30d,
                "churnRate30d": source.churn_rate_30d,
                "windowDays": eff.revenue_sync_window_days,
            },
        }
    }


async def _perform_revenue_sync(source: PortfolioRevenueSource, db: AsyncSession, window_days: int | None = None) -> None:
    """Pull fresh metrics from the connected Stripe account onto ``source`` and
    commit. Raises on Stripe failure; callers decide how to surface it."""
    window = window_days if window_days is not None else settings.revenue_sync_window_days
    since = int((_now() - timedelta(days=window)).timestamp())
    active_subscriptions = _stripe_items(stripe.Subscription.list(
        status="active",
        limit=100,
        stripe_account=source.provider_account_id,
    ))
    customers = _stripe_items(stripe.Customer.list(
        created={"gte": since},
        limit=100,
        stripe_account=source.provider_account_id,
    ))
    canceled_subscriptions = _stripe_items(stripe.Subscription.list(
        status="canceled",
        limit=100,
        stripe_account=source.provider_account_id,
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
    await db.commit()
    await db.refresh(source)


async def _sync_connected_revenue_sources(db: AsyncSession) -> int:
    """Re-sync every connected Stripe revenue source. Per-source failures are
    logged and skipped so one bad account can't stall the rest. Returns the
    number successfully synced."""
    eff = await effective_config(db)
    sources = list((await db.execute(
        select(PortfolioRevenueSource).where(
            PortfolioRevenueSource.status == "connected",
            PortfolioRevenueSource.provider_account_id != None,
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
        select(func.count(PortfolioUsageEvent.id)).where(PortfolioUsageEvent.pipeline_id == pipeline_id)
    ) or 0

    window_rows = (await db.execute(
        select(PortfolioUsageEvent.event_type, func.count(PortfolioUsageEvent.id))
        .where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= since,
        )
        .group_by(PortfolioUsageEvent.event_type)
    )).all()
    counts = {event_type: int(count) for event_type, count in window_rows}

    visitors_14d = await db.scalar(
        select(func.count(func.distinct(PortfolioUsageEvent.visitor_id))).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= since,
            PortfolioUsageEvent.visitor_id != None,
        )
    ) or 0
    active_users_14d = await db.scalar(
        select(func.count(func.distinct(func.coalesce(PortfolioUsageEvent.user_ref, PortfolioUsageEvent.visitor_id)))).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= since,
        )
    ) or 0

    daily = _empty_daily(usage_window)
    daily_rows = (await db.execute(
        select(
            func.date(PortfolioUsageEvent.occurred_at),
            PortfolioUsageEvent.event_type,
            func.count(PortfolioUsageEvent.id),
            func.count(func.distinct(PortfolioUsageEvent.visitor_id)),
        )
        .where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= since,
        )
        .group_by(func.date(PortfolioUsageEvent.occurred_at), PortfolioUsageEvent.event_type)
        .order_by(func.date(PortfolioUsageEvent.occurred_at).asc())
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
        select(PortfolioUsageEvent)
        .where(PortfolioUsageEvent.pipeline_id == pipeline_id)
        .order_by(PortfolioUsageEvent.occurred_at.desc())
        .limit(20)
    )).scalars().all())

    # Conversion funnel (14d): distinct visitors at each stage, so a few noisy
    # visitors firing many events don't distort the rates.
    stage_rows = (await db.execute(
        select(PortfolioUsageEvent.event_type, func.count(func.distinct(PortfolioUsageEvent.visitor_id)))
        .where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= since,
            PortfolioUsageEvent.visitor_id != None,
            PortfolioUsageEvent.event_type.in_(["signup", "activation"]),
        )
        .group_by(PortfolioUsageEvent.event_type)
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
        select(func.count(func.distinct(PortfolioUsageEvent.visitor_id))).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= week_start,
            PortfolioUsageEvent.visitor_id != None,
        )
    ) or 0)
    prev_week_visitors = int(await db.scalar(
        select(func.count(func.distinct(PortfolioUsageEvent.visitor_id))).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= prev_week_start,
            PortfolioUsageEvent.occurred_at < week_start,
            PortfolioUsageEvent.visitor_id != None,
        )
    ) or 0)
    this_week_signups = int(await db.scalar(
        select(func.count(PortfolioUsageEvent.id)).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= week_start,
            PortfolioUsageEvent.event_type == "signup",
        )
    ) or 0)
    prev_week_signups = int(await db.scalar(
        select(func.count(PortfolioUsageEvent.id)).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= prev_week_start,
            PortfolioUsageEvent.occurred_at < week_start,
            PortfolioUsageEvent.event_type == "signup",
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
        select(PortfolioUsageEvent.visitor_id, func.min(func.date(PortfolioUsageEvent.occurred_at)))
        .where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= retention_since,
            PortfolioUsageEvent.visitor_id != None,
        )
        .group_by(PortfolioUsageEvent.visitor_id)
    )).all()
    active_rows = (await db.execute(
        select(PortfolioUsageEvent.visitor_id, func.date(PortfolioUsageEvent.occurred_at))
        .where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= retention_since,
            PortfolioUsageEvent.visitor_id != None,
        )
        .distinct()
    )).all()
    retention = _compute_retention(
        list(first_seen_rows), list(active_rows), now.date(), window_days=retention_window_days
    )

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": source is not None,
            "totalEvents": int(total_events),
            "lastSeenAt": _iso(source.last_seen_at if source else None),
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
            "funnel": funnel,
            "growth": growth,
            "retention": retention,
            "daily": list(daily.values()),
            "recentEvents": [_serialize_usage_event(event) for event in recent_events],
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


@router.get("/{pipeline_id}/correlation")
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
            func.date(PortfolioUsageEvent.occurred_at),
            PortfolioUsageEvent.event_type,
            func.count(PortfolioUsageEvent.id),
            func.count(func.distinct(PortfolioUsageEvent.visitor_id)),
        )
        .where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= since,
        )
        .group_by(func.date(PortfolioUsageEvent.occurred_at), PortfolioUsageEvent.event_type)
    )).all()
    error_rows = (await db.execute(
        select(func.date(PortfolioErrorEvent.occurred_at), func.count(PortfolioErrorEvent.id))
        .where(
            PortfolioErrorEvent.pipeline_id == pipeline_id,
            PortfolioErrorEvent.occurred_at >= since,
        )
        .group_by(func.date(PortfolioErrorEvent.occurred_at))
    )).all()

    days = _build_correlation_days(list(usage_rows), list(error_rows), days=correlation_window, today=now.date())
    insights = _correlation_insights(days)

    revenue = await db.scalar(
        select(PortfolioRevenueSource).where(
            PortfolioRevenueSource.pipeline_id == pipeline_id,
            PortfolioRevenueSource.provider == "stripe",
        )
    )
    this_week_signups = int(await db.scalar(
        select(func.count(PortfolioUsageEvent.id)).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.event_type == "signup",
            PortfolioUsageEvent.occurred_at >= week_start,
        )
    ) or 0)
    prev_week_signups = int(await db.scalar(
        select(func.count(PortfolioUsageEvent.id)).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.event_type == "signup",
            PortfolioUsageEvent.occurred_at >= prev_week_start,
            PortfolioUsageEvent.occurred_at < week_start,
        )
    ) or 0)
    errors_14d = int(await db.scalar(
        select(func.count(PortfolioErrorEvent.id)).where(
            PortfolioErrorEvent.pipeline_id == pipeline_id,
            PortfolioErrorEvent.occurred_at >= usage_window_ago,
        )
    ) or 0)
    sessions_14d = int(await db.scalar(
        select(func.count(func.distinct(PortfolioUsageEvent.session_id))).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= usage_window_ago,
            PortfolioUsageEvent.session_id != None,
        )
    ) or 0)

    mrr = revenue.current_mrr_cents if revenue else None
    prev_mrr = revenue.previous_mrr_cents if revenue else None

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
        row = PortfolioAlertSettings(pipeline_id=pipeline_id)
        db.add(row)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    row.updated_at = _now()
    await db.commit()
    await db.refresh(row)
    return {"data": _serialize_alert_settings(AlertPrefs.resolve(row))}


@router.get("/{pipeline_id}/errors")
async def get_error_metrics(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    eff = await effective_config(db)
    source = await _usage_source(pipeline_id, db)
    usage_window = eff.analytics_usage_window_days
    since = _now() - timedelta(days=usage_window)

    total_errors = await db.scalar(
        select(func.count(PortfolioErrorEvent.id)).where(PortfolioErrorEvent.pipeline_id == pipeline_id)
    ) or 0
    errors_14d = await db.scalar(
        select(func.count(PortfolioErrorEvent.id)).where(
            PortfolioErrorEvent.pipeline_id == pipeline_id,
            PortfolioErrorEvent.occurred_at >= since,
        )
    ) or 0
    affected_sessions_14d = await db.scalar(
        select(func.count(func.distinct(PortfolioErrorEvent.session_id))).where(
            PortfolioErrorEvent.pipeline_id == pipeline_id,
            PortfolioErrorEvent.occurred_at >= since,
            PortfolioErrorEvent.session_id != None,
        )
    ) or 0
    # Denominator for a rate (not a raw count): a traffic spike shouldn't read
    # as an error spike. Uses the usage sessions already collected for the product.
    usage_sessions_14d = await db.scalar(
        select(func.count(func.distinct(PortfolioUsageEvent.session_id))).where(
            PortfolioUsageEvent.pipeline_id == pipeline_id,
            PortfolioUsageEvent.occurred_at >= since,
            PortfolioUsageEvent.session_id != None,
        )
    ) or 0
    open_issues = await db.scalar(
        select(func.count(PortfolioErrorGroup.id)).where(
            PortfolioErrorGroup.pipeline_id == pipeline_id,
            PortfolioErrorGroup.status == "unresolved",
        )
    ) or 0

    daily = _empty_error_daily(usage_window)
    daily_rows = (await db.execute(
        select(func.date(PortfolioErrorEvent.occurred_at), func.count(PortfolioErrorEvent.id))
        .where(
            PortfolioErrorEvent.pipeline_id == pipeline_id,
            PortfolioErrorEvent.occurred_at >= since,
        )
        .group_by(func.date(PortfolioErrorEvent.occurred_at))
        .order_by(func.date(PortfolioErrorEvent.occurred_at).asc())
    )).all()
    for day, count in daily_rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if key in daily:
            daily[key]["errors"] += int(count)

    top_groups = list((await db.execute(
        select(PortfolioErrorGroup)
        .where(
            PortfolioErrorGroup.pipeline_id == pipeline_id,
            PortfolioErrorGroup.last_seen_at >= since,
        )
        .order_by(PortfolioErrorGroup.event_count.desc(), PortfolioErrorGroup.last_seen_at.desc())
        .limit(10)
    )).scalars().all())

    session_rows = (await db.execute(
        select(PortfolioErrorEvent.group_id, func.count(func.distinct(PortfolioErrorEvent.session_id)))
        .where(
            PortfolioErrorEvent.pipeline_id == pipeline_id,
            PortfolioErrorEvent.occurred_at >= since,
            PortfolioErrorEvent.session_id != None,
        )
        .group_by(PortfolioErrorEvent.group_id)
    )).all()
    sessions_by_group = {group_id: int(count) for group_id, count in session_rows}

    recent_errors = list((await db.execute(
        select(PortfolioErrorEvent)
        .where(PortfolioErrorEvent.pipeline_id == pipeline_id)
        .order_by(PortfolioErrorEvent.occurred_at.desc())
        .limit(20)
    )).scalars().all())

    errors_per_session = round(int(errors_14d) / int(usage_sessions_14d), 4) if usage_sessions_14d else None

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": source is not None,
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
        }
    }


@public_router.post("/events")
async def ingest_usage_event(
    request: Request,
    body: UsageEventBody,
    db: AsyncSession = Depends(get_db),
):
    source = (await db.execute(
        select(PortfolioUsageSource).where(
            PortfolioUsageSource.pipeline_id == body.product_id,
            PortfolioUsageSource.public_key == body.key,
            PortfolioUsageSource.status == "connected",
        )
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Usage source not found")
    if not _usage_event_domain_allowed(source, request, body):
        raise HTTPException(status_code=403, detail="Usage event origin is not allowed")

    now = _now()
    event = PortfolioUsageEvent(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        event_type=body.event_type,
        visitor_id=body.visitor_id,
        session_id=body.session_id,
        user_ref=body.user_id,
        url=body.url,
        referrer=body.referrer,
        event_metadata=body.metadata or {},
        occurred_at=body.occurred_at or now,
        received_at=now,
    )
    source.last_seen_at = now
    source.updated_at = now
    db.add(event)
    await db.commit()
    return {"success": True}


@public_router.post("/errors")
async def ingest_error_event(
    request: Request,
    body: ErrorEventBody,
    db: AsyncSession = Depends(get_db),
):
    source = (await db.execute(
        select(PortfolioUsageSource).where(
            PortfolioUsageSource.pipeline_id == body.product_id,
            PortfolioUsageSource.public_key == body.key,
            PortfolioUsageSource.status == "connected",
        )
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Usage source not found")
    if not _origin_allowed(source.allowed_domain, request, body.url):
        raise HTTPException(status_code=403, detail="Error event origin is not allowed")

    now = _now()
    occurred = body.occurred_at or now
    fingerprint = _error_fingerprint(body.message, body.stack)
    title = (_normalize_error_message(body.message) or body.message)[:500]

    group = await _error_group(source.pipeline_id, fingerprint, db)
    if group is None:
        group = PortfolioErrorGroup(
            id=str(uuid.uuid4()),
            pipeline_id=source.pipeline_id,
            source_id=source.id,
            fingerprint=fingerprint,
            title=title,
            level=body.level,
            status="unresolved",
            event_count=1,
            last_release=body.release,
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
        if body.release:
            group.last_release = body.release
        # Regression: a previously resolved issue is happening again → reopen it.
        if group.status == "resolved":
            group.status = "unresolved"
        group.updated_at = now

    event = PortfolioErrorEvent(
        id=str(uuid.uuid4()),
        pipeline_id=source.pipeline_id,
        source_id=source.id,
        group_id=group.id,
        fingerprint=fingerprint,
        message=body.message[:2000],
        stack=body.stack,
        level=body.level,
        handled=body.handled,
        url=body.url,
        release=body.release,
        visitor_id=body.visitor_id,
        session_id=body.session_id,
        user_ref=body.user_id,
        event_metadata=body.metadata or {},
        occurred_at=occurred,
        received_at=now,
    )
    source.last_seen_at = now
    source.updated_at = now
    db.add(event)
    await db.commit()
    return {"success": True, "groupId": group.id, "fingerprint": fingerprint}
