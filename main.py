from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from app.config import get_settings
from app.limits import LimitReached, limit_reached_body
from fastapi.middleware.cors import CORSMiddleware
from app.routes.subscriptions import router as subscriptions_router
from app.routes.opportunities import router as opportunities_router
from app.routes.clusters import router as clusters_router
from app.routes.pipeline import router as pipeline_router
from app.routes.portfolio import router as portfolio_router
from app.routes.problems import router as problems_router
from app.routes.tasks import router as tasks_router
from app.routes.dashboard import router as dashboard_router
from app.routes.revenue_signals import router as revenue_signals_router
from app.routes.experiments import router as experiments_router
from app.routes.domain_snapshots import router as domain_snapshots_router

load_dotenv()

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    docs_url="/docs" if settings.environment == "development" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(opportunities_router)
app.include_router(subscriptions_router)
app.include_router(clusters_router)
app.include_router(pipeline_router)
app.include_router(portfolio_router)
app.include_router(problems_router)
app.include_router(tasks_router)
app.include_router(dashboard_router)
app.include_router(revenue_signals_router)
app.include_router(experiments_router)
app.include_router(domain_snapshots_router)


@app.exception_handler(LimitReached)
async def limit_reached_handler(request: Request, exc: LimitReached):
    # Render the limit payload at the top level (not under FastAPI's "detail").
    return JSONResponse(status_code=exc.status_code, content=limit_reached_body(exc.limit, exc.plan))


@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.environment}
