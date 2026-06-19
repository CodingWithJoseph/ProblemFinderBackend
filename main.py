import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from dotenv import load_dotenv
from app.config import get_settings
from fastapi.middleware.cors import CORSMiddleware
from app.routes.subscriptions import router as subscriptions_router
from app.routes.clusters import router as clusters_router
from app.routes.pipeline import router as pipeline_router
from app.routes.portfolio import public_router as portfolio_public_router
from app.routes.portfolio import router as portfolio_router
from app.routes.problems import router as problems_router
from app.routes.tasks import router as tasks_router
from app.routes.teams import router as teams_router
from app.routes.teams import invites_router as invites_router
from app.routes.issues import router as issues_router
from app.routes.dashboard import router as dashboard_router
from app.routes.homepage import router as homepage_router
from app.routes.public import router as public_router

load_dotenv()

settings = get_settings()
logger = logging.getLogger(__name__)


def _start_background_scheduler():
    """Start periodic background jobs (revenue sync, alert checks), or return
    None when disabled/unavailable.

    Best-effort: a missing apscheduler simply skips scheduling rather than
    failing app startup. Individual jobs are skipped when their interval is 0.
    """
    revenue_interval = settings.revenue_sync_interval_hours
    alert_interval = settings.alert_check_interval_hours
    if revenue_interval <= 0 and alert_interval <= 0:
        return None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError:  # pragma: no cover - depends on deploy env
        logger.warning("apscheduler not installed; background jobs disabled")
        return None

    from app.services.locks import ALERT_CHECKS_LOCK, REVENUE_SYNC_LOCK, run_with_advisory_lock

    scheduler = AsyncIOScheduler()
    if revenue_interval > 0:
        from app.routes.portfolio import run_scheduled_revenue_sync

        async def _locked_revenue_sync():
            await run_with_advisory_lock(REVENUE_SYNC_LOCK, run_scheduled_revenue_sync, name="revenue_sync")

        scheduler.add_job(_locked_revenue_sync, "interval", hours=revenue_interval, id="revenue_sync")
        logger.info("scheduled revenue sync every %s hour(s)", revenue_interval)
    if alert_interval > 0:
        from app.services.alerts import run_alert_checks

        async def _locked_alert_checks():
            await run_with_advisory_lock(ALERT_CHECKS_LOCK, run_alert_checks, name="alert_checks")

        scheduler.add_job(_locked_alert_checks, "interval", hours=alert_interval, id="alert_checks")
        logger.info("scheduled alert checks every %s hour(s)", alert_interval)
    scheduler.start()
    return scheduler


@asynccontextmanager
async def lifespan(app: "FastAPI"):
    scheduler = _start_background_scheduler()
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    docs_url="/docs" if settings.environment == "development" else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(subscriptions_router)
app.include_router(clusters_router)
app.include_router(pipeline_router)
app.include_router(portfolio_router)
app.include_router(portfolio_public_router)
app.include_router(problems_router)
app.include_router(tasks_router)
app.include_router(teams_router)
app.include_router(invites_router)
app.include_router(issues_router)
app.include_router(dashboard_router)
app.include_router(homepage_router)
app.include_router(public_router)


@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.environment}
