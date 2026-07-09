from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MonitorCustomer, MonitorRevenueSource, MonitorUsageSource, Pipeline
from app.services.monitoring.common import _clean_text, _domain_from_url, _origin_allowed


def _selected_mrr(source: MonitorRevenueSource | None, engine: str) -> tuple[int | None, int | None]:
    """Return current/previous MRR for the configured revenue engine."""
    if not source:
        return None, None
    if engine == "invoice":
        return source.invoice_mrr_cents, source.previous_invoice_mrr_cents
    return source.current_mrr_cents, source.previous_mrr_cents


async def _revenue_source(pipeline_id: str, db: AsyncSession, provider: str = "stripe") -> MonitorRevenueSource | None:
    return (await db.execute(
        select(MonitorRevenueSource)
        .where(
            MonitorRevenueSource.pipeline_id == pipeline_id,
            MonitorRevenueSource.provider == provider,
        )
        .order_by(MonitorRevenueSource.created_at.asc())
    )).scalar_one_or_none()


def _usage_event_domain_allowed(source: MonitorUsageSource, request: Request, body) -> bool:
    return _origin_allowed(source.allowed_domain, request, body.url)


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
