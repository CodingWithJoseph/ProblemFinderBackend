"""Server-side tier limits and enforcement.

Single source of truth for plan limits (mirrors lib/config.ts on the web).
Limit violations raise LimitReached, which the handler in main.py renders as a
top-level JSON body:

    {"error": "limit_reached", "limit": "<key>", "plan": "<plan>", "upgrade_required": "pro"}

Rate limits use HTTP 429; plan/count limits use HTTP 403.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PlanEnum, Subscription, Problem, Task, SearchUsage

# None means unlimited.
TIER_LIMITS: dict[PlanEnum, dict] = {
    PlanEnum.free:  {"searches_per_24h": 3,    "max_pipeline_cards": 3,    "max_problems_per_pipeline": 10,   "max_tasks_per_pipeline": 10,   "ai_clusters": False},
    PlanEnum.pro:   {"searches_per_24h": None, "max_pipeline_cards": None, "max_problems_per_pipeline": None, "max_tasks_per_pipeline": None, "ai_clusters": True},
    PlanEnum.elite: {"searches_per_24h": None, "max_pipeline_cards": None, "max_problems_per_pipeline": None, "max_tasks_per_pipeline": None, "ai_clusters": True},
    PlanEnum.admin: {"searches_per_24h": None, "max_pipeline_cards": None, "max_problems_per_pipeline": None, "max_tasks_per_pipeline": None, "ai_clusters": True},
}

# Free users upgrade to Pro to unlock everything.
UPGRADE_TARGET = "pro"


class LimitReached(Exception):
    """Raised when a request exceeds the caller's plan limit."""

    def __init__(self, limit: str, plan: PlanEnum, status_code: int):
        self.limit = limit
        self.plan = plan
        self.status_code = status_code
        super().__init__(f"limit_reached:{limit}")


def limit_reached_body(limit: str, plan: PlanEnum) -> dict:
    return {
        "error": "limit_reached",
        "limit": limit,
        "plan": plan.value if isinstance(plan, PlanEnum) else str(plan),
        "upgrade_required": UPGRADE_TARGET,
    }


def register_limit_handler(app) -> None:
    """Register the LimitReached -> top-level-JSON handler on a FastAPI app.

    Shared by the real app (main.py) and the test app (conftest) so both render
    limit_reached identically.
    """
    from fastapi.responses import JSONResponse

    @app.exception_handler(LimitReached)
    async def _limit_reached_handler(request, exc: LimitReached):
        return JSONResponse(status_code=exc.status_code, content=limit_reached_body(exc.limit, exc.plan))


def limit_for(plan: PlanEnum, key: str):
    return TIER_LIMITS.get(plan, TIER_LIMITS[PlanEnum.free]).get(key)


def ai_clusters_allowed(plan: PlanEnum) -> bool:
    return bool(limit_for(plan, "ai_clusters"))


def is_pro_plus(plan: PlanEnum) -> bool:
    """True for paid tiers (pro / elite / admin); False for free."""
    return plan != PlanEnum.free


def is_elite(plan: PlanEnum) -> bool:
    """True only for the elite (and admin) tier."""
    return plan in (PlanEnum.elite, PlanEnum.admin)


async def get_plan(uid: str, db: AsyncSession) -> PlanEnum:
    res = await db.execute(select(Subscription).where(Subscription.uid == uid))
    sub = res.scalar_one_or_none()
    return sub.plan if sub else PlanEnum.free


async def enforce_search_limit(uid: str, plan: PlanEnum, db: AsyncSession) -> None:
    cap = limit_for(plan, "searches_per_24h")
    if cap is None:
        return
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    res = await db.execute(
        select(func.count()).where(
            SearchUsage.uid == uid,
            SearchUsage.searched_at >= since,
        )
    )
    if (res.scalar() or 0) >= cap:
        raise LimitReached("search", plan, status_code=429)


async def enforce_problems_limit(uid: str, plan: PlanEnum, pipeline_id: str, db: AsyncSession) -> None:
    cap = limit_for(plan, "max_problems_per_pipeline")
    if cap is None:
        return
    res = await db.execute(
        select(func.count()).where(
            Problem.pipeline_id == pipeline_id, Problem.user_id == uid
        )
    )
    if (res.scalar() or 0) >= cap:
        raise LimitReached("problems", plan, status_code=403)


async def enforce_tasks_limit(uid: str, plan: PlanEnum, pipeline_id: str, db: AsyncSession) -> None:
    cap = limit_for(plan, "max_tasks_per_pipeline")
    if cap is None:
        return
    res = await db.execute(
        select(func.count()).where(
            Task.pipeline_id == pipeline_id, Task.user_id == uid
        )
    )
    if (res.scalar() or 0) >= cap:
        raise LimitReached("tasks", plan, status_code=403)
