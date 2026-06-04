"""Async endpoint / CRUD tests.

Skipped unless TEST_DATABASE_URL points at a throwaway Postgres — the models use
pgvector `vector`, `JSONB`, `TSVECTOR`, and `ARRAY` columns that SQLite cannot
create. Requires `pytest-asyncio` and `httpx` (importorskip skips the module if
they are absent, so the default CI run never errors here).
"""
import os

import pytest

pytest.importorskip("pytest_asyncio")
pytest.importorskip("httpx")

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL (throwaway Postgres) to run endpoint tests",
)

pytest_asyncio = pytest.importorskip("pytest_asyncio")


@pytest_asyncio.fixture
async def client():
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    import app.db as db
    from app.db import Base
    import app.auth as auth_mod
    from main import app

    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_db():
        async with sessionmaker() as s:
            yield s

    app.dependency_overrides[db.get_db] = _get_db
    app.dependency_overrides[auth_mod.get_uid] = lambda: "test-uid"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()
    await engine.dispose()


async def test_dashboard_summary(client):
    r = await client.get("/dashboard/summary")
    assert r.status_code == 200
    assert "clustersTracked" in r.json()
