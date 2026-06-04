from fastapi import FastAPI
from dotenv import load_dotenv
from app.config import get_settings
from app.limits import register_limit_handler
from fastapi.middleware.cors import CORSMiddleware
from app.routes.subscriptions import router as subscriptions_router
from app.routes.opportunities import router as opportunities_router
from app.routes.clusters import router as clusters_router
from app.routes.pipeline import router as pipeline_router
from app.routes.portfolio import router as portfolio_router
from app.routes.problems import router as problems_router
from app.routes.tasks import router as tasks_router
from app.routes.dashboard import router as dashboard_router
from app.routes.domain_snapshots import router as domain_snapshots_router
from app.routes.homepage import router as homepage_router
from app.routes.public import router as public_router

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
app.include_router(domain_snapshots_router)
app.include_router(homepage_router)
app.include_router(public_router)


register_limit_handler(app)


@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.environment}
