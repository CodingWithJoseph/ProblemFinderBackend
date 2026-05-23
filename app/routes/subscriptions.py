from fastapi import APIRouter, Depends, HTTPException, Request, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from app.db import get_db
from app.models import Subscription, PlanEnum
from app.auth import get_uid, get_firestore
from app.config import get_settings
from datetime import datetime, timezone
import stripe

settings = get_settings()
stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


# ─── Helpers ────────────────────────────────────────────────────────────────

def _get_plan_from_price_id(price_id: str) -> PlanEnum:
    price_map = {
        settings.stripe_pro_monthly_price_id: PlanEnum.pro,
        settings.stripe_pro_yearly_price_id: PlanEnum.pro,
        settings.stripe_elite_monthly_price_id: PlanEnum.elite,
        settings.stripe_elite_yearly_price_id: PlanEnum.elite,
    }
    return price_map.get(price_id, PlanEnum.free)


def _get_price_id(price_key: str) -> str | None:
    price_map = {
        "pro_monthly": settings.stripe_pro_monthly_price_id,
        "pro_yearly": settings.stripe_pro_yearly_price_id,
        "elite_monthly": settings.stripe_elite_monthly_price_id,
        "elite_yearly": settings.stripe_elite_yearly_price_id,
    }
    return price_map.get(price_key)


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


def _write_plan_to_firestore(uid: str, plan: str):
    try:
        fs = get_firestore()
        fs.collection('users').document(uid).update({'plan': plan})
    except Exception as e:
        print(f"Firestore write failed for uid {uid}: {e}")


def _get_stripe_attr(obj, key: str, default=None):
    """Safely get attribute from either StripeObject or dict."""
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)
    except Exception:
        return default


# ─── Get Subscription ───────────────────────────────────────────────────────

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


# ─── Checkout ────────────────────────────

class CheckoutRequest(BaseModel):
    price_key: str


@router.post("/checkout")
async def create_checkout(
        body: CheckoutRequest,
        uid: str = Depends(get_uid),
        db: AsyncSession = Depends(get_db),  # ← add db
):
    price_id = _get_price_id(body.price_key)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid price key")

    result = await db.execute(
        select(Subscription).where(Subscription.uid == uid)
    )
    sub = result.scalar_one_or_none()
    existing_customer_id = sub.stripe_customer_id if sub else None

    session_params = dict(
        mode="subscription",
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{settings.app_url}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.app_url}/payment/cancel",
        client_reference_id=uid,
        metadata={"uid": uid, "price_key": body.price_key},
    )

    if existing_customer_id:
        session_params["customer"] = existing_customer_id  # ← reuse, Stripe won't create a new one

    session = stripe.checkout.Session.create(**session_params)
    return {"url": session.url}


# ─── Portal ─────────────────────────────────────────────────────────────────

@router.post("/portal")
async def create_portal(
    uid: str = Depends(get_uid),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Subscription).where(Subscription.uid == uid)
    )
    sub = result.scalar_one_or_none()

    if not sub or not sub.stripe_customer_id:
        raise HTTPException(status_code=404, detail="No subscription found")

    session = stripe.billing_portal.Session.create(
        customer=sub.stripe_customer_id,
        return_url=f"{settings.app_url}/dashboard/settings",
    )

    return {"url": session.url}


# ─── Upgrade ────────────────────────────────────────────────────────────────

class UpgradeRequest(BaseModel):
    price_key: str


@router.post("/upgrade")
async def upgrade_subscription(
    body: UpgradeRequest,
    uid: str = Depends(get_uid),
    db: AsyncSession = Depends(get_db),
):
    price_id = _get_price_id(body.price_key)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid price key")

    result = await db.execute(
        select(Subscription).where(Subscription.uid == uid)
    )
    sub = result.scalar_one_or_none()

    if not sub or not sub.stripe_subscription_id:
        raise HTTPException(status_code=404, detail="No active subscription found")

    subscription = stripe.Subscription.retrieve(sub.stripe_subscription_id)

    stripe.Subscription.modify(
        sub.stripe_subscription_id,
        items=[{
            "id": subscription.items.data[0].id,
            "price": price_id,
        }],
        proration_behavior="always_invoice",
        metadata={"uid": uid, "price_key": body.price_key},
    )

    return {"success": True}


# ─── Webhook ────────────────────────────────────────────────────────────────

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

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(data, db)
    elif event_type == "customer.subscription.updated":
        await _sync_subscription(data, db)
    elif event_type == "customer.subscription.deleted":
        await _handle_cancellation(data, db)

    return {"received": True}


async def _handle_checkout_completed(session, db: AsyncSession):
    uid = session.metadata["uid"] if session.metadata else None
    if not uid:
        return

    stripe_sub = stripe.Subscription.retrieve(session.subscription)
    await _sync_subscription(stripe_sub, db, uid=uid,
                             customer_id=session.customer)


async def _get_uid_from_stripe_sub(stripe_sub, db: AsyncSession) -> str | None:
    print(f"GET_UID: stripe_sub type={type(stripe_sub)}")
    print(f"GET_UID: is_dict={isinstance(stripe_sub, dict)}")

    try:
        metadata = stripe_sub.metadata if not isinstance(stripe_sub, dict) else stripe_sub.get("metadata", {})
        print(f"GET_UID: metadata={metadata}")
        if metadata and "uid" in metadata:
            print(f"GET_UID: found uid in metadata={metadata['uid']}")
            return metadata["uid"]
    except Exception as e:
        print(f"GET_UID: metadata error={e}")

    try:
        customer = stripe_sub.customer if not isinstance(stripe_sub, dict) else stripe_sub.get("customer")
        print(f"GET_UID: looking up by customer={customer}")
        if customer:
            result = await db.execute(
                select(Subscription).where(Subscription.stripe_customer_id == customer)
            )
            existing = result.scalar_one_or_none()
            print(f"GET_UID: found by customer={existing}")
            if existing:
                return existing.uid
    except Exception as e:
        print(f"GET_UID: customer lookup error={e}")

    print("GET_UID: no uid found")
    return None


async def _sync_subscription(
        stripe_sub,
        db: AsyncSession,
        uid: str = None,
        customer_id: str = None,
):
    print(f"SYNC: starting uid={uid}")

    if not uid:
        uid = await _get_uid_from_stripe_sub(stripe_sub, db)

    print(f"SYNC: resolved uid={uid}")

    if not uid:
        print("SYNC: no uid, returning early")
        return

    result = await db.execute(
        select(Subscription).where(Subscription.uid == uid)
    )
    sub = result.scalar_one_or_none()
    print(f"SYNC: found sub={sub is not None}")

    if not sub:
        sub = Subscription(uid=uid)
        db.add(sub)
        print("SYNC: created new sub")

    is_object = not isinstance(stripe_sub, dict)
    print(f"SYNC: is_object={is_object}")

    if is_object:
        price_id = stripe_sub.items.data[0].price.id
        cancel_at_period_end = stripe_sub.cancel_at_period_end
        current_period_end = stripe_sub.items.data[0].current_period_end
        customer = stripe_sub.customer
        subscription_id = stripe_sub.id
    else:
        price_id = stripe_sub["items"]["data"][0]["price"]["id"]
        cancel_at_period_end = stripe_sub["cancel_at_period_end"]
        current_period_end = stripe_sub["current_period_end"]
        customer = stripe_sub["customer"]
        subscription_id = stripe_sub["id"]

    print(f"SYNC: price_id={price_id} cancel_at_period_end={cancel_at_period_end}")

    plan = _get_plan_from_price_id(price_id)
    print(f"SYNC: plan={plan}")

    sub.stripe_subscription_id = subscription_id
    sub.stripe_customer_id = customer_id or customer
    sub.stripe_price_id = price_id
    sub.plan = plan
    sub.cancel_at_period_end = cancel_at_period_end
    sub.current_period_end = datetime.fromtimestamp(
        current_period_end, tz=timezone.utc
    )

    await db.flush()
    print(f"SYNC: flushed to db")

    plan_value = plan.value if hasattr(plan, 'value') else str(plan)
    _write_plan_to_firestore(uid, plan_value)
    print(f"SYNC: wrote to firestore plan={plan_value}")


async def _handle_cancellation(stripe_sub, db: AsyncSession):
    uid = await _get_uid_from_stripe_sub(stripe_sub, db)
    if not uid:
        return

    result = await db.execute(
        select(Subscription).where(Subscription.uid == uid)
    )
    sub = result.scalar_one_or_none()
    if sub:
        sub.plan = PlanEnum.free
        sub.stripe_subscription_id = None
        sub.stripe_price_id = None
        sub.cancel_at_period_end = False
        sub.current_period_end = None

        await db.flush()
        _write_plan_to_firestore(uid, 'free')