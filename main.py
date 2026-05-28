from fastapi import FastAPI
from dotenv import load_dotenv
from app.config import get_settings
from fastapi.middleware.cors import CORSMiddleware
from app.routes.subscriptions import router as subscriptions_router
from app.routes.opportunities import router as opportunities_router
from app.routes.clusters import router as clusters_router
from app.routes.pipeline import router as pipeline_router
from app.routes.portfolio import router as portfolio_router

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


@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.environment}