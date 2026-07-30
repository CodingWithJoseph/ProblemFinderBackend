import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routes.clusters import router as clusters_router
from app.routes.pipeline import router as pipeline_router
from app.routes.problems import router as problems_router
from app.routes.public import router as public_router
from app.routes.search_sessions import router as search_sessions_router
from app.routes.signal_workspace import router as signal_workspace_router

load_dotenv()

settings = get_settings()


@asynccontextmanager
async def lifespan(app: "FastAPI"):
    signal_stop = asyncio.Event()
    signal_worker = None
    if settings.signal_analysis_worker_enabled:
        from app.services.signal_analysis_worker import run_signal_worker

        signal_worker = asyncio.create_task(
            run_signal_worker(signal_stop, settings=settings),
            name="signal-analysis-worker",
        )
    try:
        yield
    finally:
        signal_stop.set()
        if signal_worker:
            signal_worker.cancel()
            try:
                await signal_worker
            except asyncio.CancelledError:
                pass


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

app.include_router(clusters_router)
app.include_router(search_sessions_router)
app.include_router(signal_workspace_router)
app.include_router(pipeline_router)
app.include_router(problems_router)
app.include_router(public_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "environment": settings.environment,
    }
