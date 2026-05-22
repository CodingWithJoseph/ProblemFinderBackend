from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import get_settings
from app.routes.pipeline import router as pipeline_router
from app.routes.portfolio import router as portfolio_router
from app.routes.subscriptions import router as subscriptions_router
from app.routes.opportunities import router as opportunities_router


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

app.include_router(pipeline_router)
app.include_router(portfolio_router)
app.include_router(subscriptions_router)

app.include_router(opportunities_router)


@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.environment}

@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.environment}


import uvicorn
if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
