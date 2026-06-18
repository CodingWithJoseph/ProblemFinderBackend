import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.auth import get_uid
from app.models import Pipeline, PortfolioRevenueSource, PortfolioUsageEvent, PortfolioUsageSource
from app.routes.pipeline import (
    _fetch_issue_counts_for_cards,
    _fetch_posts_for_cards,
    _fetch_teams_for_cards,
    _serialize_cards,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])
public_router = APIRouter(prefix="/public/portfolio", tags=["public-portfolio"])

UsageEventType = Literal["pageview", "signup", "login", "activation", "custom"]
RevenueProvider = Literal["stripe"]


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


class UsageSourceUpdateBody(BaseModel):
    product_url: str | None = Field(default=None, max_length=2048)
    allowed_domain: str | None = Field(default=None, max_length=255)


class RevenueSourceBody(BaseModel):
    provider: RevenueProvider = "stripe"


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


def _serialize_revenue_source(source: PortfolioRevenueSource | None) -> dict | None:
    if not source:
        return None
    return {
        "id": source.id,
        "pipelineId": source.pipeline_id,
        "provider": source.provider,
        "status": source.status,
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


def _usage_event_domain_allowed(source: PortfolioUsageSource, request: Request, body: UsageEventBody) -> bool:
    if not source.allowed_domain:
        return True
    candidates = [
        request.headers.get("origin"),
        request.headers.get("referer"),
        body.url,
    ]
    return any(_host_matches_allowed(candidate, source.allowed_domain) for candidate in candidates)


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


@router.get("/{pipeline_id}/revenue")
async def get_revenue_metrics(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
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
            },
        }
    }


@router.get("/{pipeline_id}/usage")
async def get_usage_metrics(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _require_launched_product(pipeline_id, db, uid)
    source = await _usage_source(pipeline_id, db)
    since = _now() - timedelta(days=14)

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

    daily = _empty_daily()
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

    return {
        "data": {
            "source": _serialize_usage_source(source),
            "connected": source is not None,
            "totalEvents": int(total_events),
            "lastSeenAt": _iso(source.last_seen_at if source else None),
            "summary14d": {
                "pageviews": counts.get("pageview", 0),
                "visitors": int(visitors_14d),
                "signups": counts.get("signup", 0),
                "logins": counts.get("login", 0),
                "activations": counts.get("activation", 0),
                "customEvents": counts.get("custom", 0),
                "activeUsers": int(active_users_14d),
            },
            "daily": list(daily.values()),
            "recentEvents": [_serialize_usage_event(event) for event in recent_events],
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
