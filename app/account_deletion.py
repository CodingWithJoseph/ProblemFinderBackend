"""Retryable deletion after a durable application-account deny fence."""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select, update

from app.auth import supabase_admin_user
from app.db import AsyncSessionLocal
from app.models import AccountProfile, Pipeline, Problem, SearchSession, SignalAnalysisCase, SolutionCase

logger = logging.getLogger(__name__)


async def finish_account_deletion(db, uid: str) -> None:
    profile = (await db.execute(
        select(AccountProfile).where(AccountProfile.id == uid).with_for_update()
    )).scalar_one()
    if profile.deletion_completed_at is not None:
        return
    if profile.deleted_at is None:
        raise ValueError("Account must be fenced before deletion")
    await supabase_admin_user(uid, delete=True)
    # Child versions/jobs/turns/tasks cascade. Explicit cases cover orphaned data.
    for model in (Problem, SignalAnalysisCase, SolutionCase, Pipeline, SearchSession):
        await db.execute(delete(model).where(model.user_id == uid))
    await db.execute(update(AccountProfile).where(AccountProfile.id == uid).values(
        deletion_completed_at=datetime.now(timezone.utc), onboarding_complete=False,
    ))
    await db.commit()


async def run_account_deletion_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with AsyncSessionLocal() as db:
                pending = (await db.execute(select(AccountProfile.id).where(
                    AccountProfile.deleted_at != None, AccountProfile.deletion_completed_at == None,
                ).limit(20))).scalars().all()
            for uid in pending:
                if stop.is_set():
                    break
                async with AsyncSessionLocal() as db:
                    try:
                        await finish_account_deletion(db, uid)
                    except Exception:
                        await db.rollback()
                        logger.warning("Account deletion remains pending; retry scheduled")
        except Exception:
            logger.warning("Account deletion worker is unavailable; retry scheduled")
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
