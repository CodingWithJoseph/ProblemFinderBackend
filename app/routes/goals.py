"""Goals + onboarding routes for the portfolio.

Split out of ``portfolio.py`` to keep that module focused. Mounted under the same
``/portfolio`` prefix, but **included before** the main portfolio router in
``main.py`` so the static ``/goals`` and ``/getting-started`` paths win over the
portfolio router's ``/{pipeline_id}`` catch-all.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.db import get_db
from app.routes.portfolio_common import require_owned_product
from app.services.getting_started import getting_started_payload
from app.services.goals import account_goals_payload, project_goals_payload

router = APIRouter(prefix="/portfolio", tags=["portfolio-goals"])


@router.get("/goals")
async def get_account_goals(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Portfolio-level (account-scoped) goals with progress + milestone log.
    Reconciles achievements on read. Mounted before /{pipeline_id} so the static
    path wins."""
    return {"data": await account_goals_payload(uid, db)}


@router.get("/getting-started")
async def get_getting_started(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Ordered onboarding checklist + the next action to take. Guides a new
    account through the landscape (discover → build → launch → monitor) before
    the numeric goals have data. Static path, so mounted before /{pipeline_id}."""
    return {"data": await getting_started_payload(uid, db)}


@router.get("/{pipeline_id}/goals")
async def get_project_goals(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Project-scoped goals with progress + milestone log. Pre-launch returns the
    journey goals (problems/features/build); once launched the outcome goals
    (signups/revenue/traffic/setup/issues) join them."""
    product = await require_owned_product(pipeline_id, db, uid)
    return {"data": await project_goals_payload(pipeline_id, db, launched=product.launched_at is not None)}
