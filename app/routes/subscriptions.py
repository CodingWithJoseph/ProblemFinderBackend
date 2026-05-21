from fastapi import APIRouter, Depends, HTTPException, Request, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.models import Subscription, PlanEnum
from app.auth import get_uid
from app.config import get_settings
import stripe

settings = get_settings()
stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get("/{uid}")
async def get_subscription(
    uid: str,
    db: AsyncSession = Depends(get_db),
    caller_uid: str = Depends(get_uid),
):
    if uid != caller_uid:
        raise HTTPException(status_code=403, detail="Forbidden")

    result = await db.execute(
        select(Subscription).where(Subscription.uid == uid)
    )
    sub = result.scalar_one_or_none()

    if not sub:
        return {
            "uid": uid,
            "plan": PlanEnum.free,
            "stripeCustomerId": None,
            "stripeSubscriptionId": None,
            "stripePriceId": None,
            "currentPeriodEnd": None,
            "cancelAtPeriodEnd": False,
        }

    return _serialize(sub)


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, settings.stripe_webhook_secret
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "customer.subscription.updated":
        await _sync_subscription(event["data"]["object"], db)
    elif event["type"] == "customer.subscription.deleted":
        await _handle_cancellation(event["data"]["object"], db)

    return {"received": True}


async def _sync_subscription(stripe_sub: dict, db: AsyncSession):
    uid = stripe_sub["metadata"].get("uid")
    if not uid:
        return

    result = await db.execute(select(Subscription).where(Subscription.uid == uid))
    sub = result.scalar_one_or_none()

    if not sub:
        sub = Subscription(uid=uid)
        db.add(sub)

    sub.stripe_subscription_id = stripe_sub["id"]
    sub.stripe_customer_id = stripe_sub["customer"]
    sub.stripe_price_id = stripe_sub["items"]["data"][0]["price"]["id"]
    sub.cancel_at_period_end = stripe_sub["cancel_at_period_end"]

    from datetime import datetime
    sub.current_period_end = datetime.utcfromtimestamp(stripe_sub["current_period_end"])


async def _handle_cancellation(stripe_sub: dict, db: AsyncSession):
    uid = stripe_sub["metadata"].get("uid")
    if not uid:
        return

    result = await db.execute(select(Subscription).where(Subscription.uid == uid))
    sub = result.scalar_one_or_none()
    if sub:
        sub.plan = PlanEnum.free
        sub.stripe_subscription_id = None
        sub.stripe_price_id = None
        sub.cancel_at_period_end = False
        sub.current_period_end = None


def _serialize(s: Subscription) -> dict:
    return {
        "uid": s.uid,
        "plan": s.plan,
        "stripeCustomerId": s.stripe_customer_id,
        "stripeSubscriptionId": s.stripe_subscription_id,
        "stripePriceId": s.stripe_price_id,
        "currentPeriodEnd": s.current_period_end,
        "cancelAtPeriodEnd": s.cancel_at_period_end,
    }