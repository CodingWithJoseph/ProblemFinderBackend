"""Cross-worker coordination via Postgres advisory locks.

The in-process scheduler (see ``main._start_background_scheduler``) runs in every
API worker. When the app is deployed with more than one worker/instance, each
would fire the same periodic job on every tick. Wrapping a job in
``run_with_advisory_lock`` means exactly one worker acquires the lock and does
the work; the others skip that tick. With a single worker it's a cheap no-op.

Locks are session-level on a dedicated connection, held for the job's duration
and released explicitly (and again on connection close as a backstop).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, TypeVar

from sqlalchemy import text

from app.db import engine

logger = logging.getLogger(__name__)

# Stable, arbitrary 32-bit keys — one per periodic job. Changing these would
# let an old and new worker run the same job concurrently during a rollout.
REVENUE_SYNC_LOCK = 4820001
ALERT_CHECKS_LOCK = 4820002
ALERT_DIGEST_LOCK = 4820003

T = TypeVar("T")


async def run_with_advisory_lock(lock_key: int, job: Callable[[], Awaitable[T]], *, name: str = "job") -> T | None:
    """Run ``job`` only if this process can grab the given advisory lock.

    Returns the job's result, or ``None`` if the lock was busy (another worker
    is running it) and the job was skipped.
    """
    async with engine.connect() as conn:
        acquired = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key})).scalar()
        if not acquired:
            logger.info("%s: advisory lock %s held by another worker; skipping this tick", name, lock_key)
            return None
        try:
            return await job()
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
            await conn.commit()
