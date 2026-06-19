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


def _start_revenue_scheduler():
    """Start the periodic revenue-sync job, or return None when disabled/unavailable.

    Best-effort: a missing apscheduler or a zero interval simply skips it rather
    than failing app startup.
    """
    interval = settings.revenue_sync_interval_hours
    if interval and interval <= 0:
        return None
    if not interval:
        return None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError:  # pragma: no cover - depends on deploy env
        logger.warning("apscheduler not installed; scheduled revenue sync disabled")
        return None

    from app.routes.portfolio import run_scheduled_revenue_sync

    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_scheduled_revenue_sync, "interval", hours=interval, id="revenue_sync")
    scheduler.start()
    logger.info("scheduled revenue sync every %s hour(s)", interval)
    return scheduler


@asynccontextmanager
async def lifespan(app: "FastAPI"):
    scheduler = _start_revenue_scheduler()
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
