"""Opt-in Browse tests against an isolated local PostgreSQL database."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import get_uid
from app.db import Base, get_db
from app.models import Cluster, ClusterItem, Pipeline
from app.routes.clusters import router


DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="Set TEST_DATABASE_URL to isolated local PostgreSQL"
)

OBSERVED = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
VISIBLE = [
    (30, 3, OBSERVED),
    (29, 2, OBSERVED),
    (28, 2, OBSERVED),
    (20, 8, OBSERVED - timedelta(days=1)),
    (12, 4, None),
    (11, 2, None),
    (10, 2, None),
]
EXPECTED_IDS = [30, 29, 28, 20, 12, 11, 10]


@pytest_asyncio.fixture
async def browse_postgres():
    url = urlparse(DSN.replace("postgresql+asyncpg", "postgresql"))
    assert url.hostname in {"127.0.0.1", "localhost"} and url.path == "/auth_migration", (
        "Refusing a non-test database"
    )
    schema = f"browse_test_{uuid.uuid4().hex}"
    admin = create_async_engine(DSN)
    engine = None
    schema_created = False
    try:
        async with admin.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        engine = create_async_engine(
            DSN,
            connect_args={"server_settings": {"search_path": f"{schema},public"}},
        )
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[Cluster.__table__, ClusterItem.__table__, Pipeline.__table__],
                )
            )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            for cluster_id, count, observed in VISIBLE:
                db.add(Cluster(
                    id=cluster_id,
                    name=f"Cluster {cluster_id}",
                    summary=None,
                    pipeline_version="v2",
                ))
                for index in range(count):
                    db.add(ClusterItem(
                        id=str(uuid.UUID(int=cluster_id * 100 + index)),
                        cluster_id=cluster_id,
                        pipeline_version="v2",
                        score=index,
                        posted_at=observed,
                        problem_statement=f"statement {index}",
                    ))
            db.add_all([
                Cluster(id=999, name="Old cluster", pipeline_version="v1"),
                Cluster(id=998, name="Only old items", pipeline_version="v2"),
                Cluster(id=997, name="Empty", pipeline_version="v2"),
                ClusterItem(cluster_id=999, pipeline_version="v2", posted_at=OBSERVED),
                ClusterItem(cluster_id=998, pipeline_version="v1", posted_at=OBSERVED),
                ClusterItem(cluster_id=None, pipeline_version="v2", posted_at=OBSERVED),
                ClusterItem(
                    cluster_id=29,
                    pipeline_version="v1",
                    score=100,
                    posted_at=OBSERVED + timedelta(days=1),
                    problem_statement="An old pipeline item",
                ),
                Pipeline(user_id="alice", name="Active", source_cluster_id="29"),
                Pipeline(user_id="alice", name="Duplicate", source_cluster_id="29"),
                Pipeline(
                    user_id="alice", name="Later stage", source_cluster_id="12", stage="building"
                ),
                Pipeline(
                    user_id="alice", name="Removed", source_cluster_id="20", removed_at=OBSERVED
                ),
                Pipeline(user_id="bob", name="Other user", source_cluster_id="28"),
            ])
            await db.commit()

        app = FastAPI()
        app.include_router(router)

        async def database_override():
            async with sessions() as db:
                yield db

        async def user_override(request: Request):
            return request.headers.get("x-test-user", "alice")

        app.dependency_overrides[get_db] = database_override
        app.dependency_overrides[get_uid] = user_override
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client, sessions
    finally:
        if engine is not None:
            await engine.dispose()
        if schema_created:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.parametrize("limit", [1, 2, 3, 7, 20, 50])
async def test_browse_traverses_ties_and_null_dates(browse_postgres, limit):
    client, _ = browse_postgres
    seen = []
    cursor = None
    for _ in range(10):
        params = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/clusters/browse/page", params=params)
        assert response.status_code == 200
        page = response.json()
        assert len(page["clusters"]) <= limit
        for cluster in page["clusters"]:
            seen.append(cluster["id"])
            _, count, observed = next(row for row in VISIBLE if row[0] == cluster["id"])
            assert cluster["postCount"] == count
            expected_date = observed.isoformat().replace("+00:00", "Z") if observed else None
            assert cluster["date"] == expected_date
            assert cluster["problemStatement"] == f"statement {count - 1}"
            assert cluster["isWatched"] == (cluster["id"] in {29, 12})
        cursor = page["nextCursor"]
        assert (cursor is not None) == (len(seen) < len(EXPECTED_IDS))
        if cursor is None:
            break
    else:
        pytest.fail("Pagination did not terminate")
    assert seen == EXPECTED_IDS


async def test_browse_isolates_watched_state(browse_postgres):
    client, _ = browse_postgres
    response = await client.get("/clusters/browse/page", headers={"x-test-user": "bob"})
    assert response.status_code == 200
    assert {row["id"] for row in response.json()["clusters"] if row["isWatched"]} == {28}


async def test_browse_allows_page_size_changes(browse_postgres):
    client, _ = browse_postgres
    first = await client.get("/clusters/browse/page", params={"limit": 2})
    assert first.status_code == 200
    second = await client.get(
        "/clusters/browse/page", params={"limit": 3, "cursor": first.json()["nextCursor"]}
    )
    assert second.status_code == 200
    assert [row["id"] for row in second.json()["clusters"]] == [28, 20, 12]


async def test_browse_empty_dataset(browse_postgres):
    client, sessions = browse_postgres
    async with sessions() as db:
        await db.execute(delete(ClusterItem))
        await db.commit()
    response = await client.get("/clusters/browse/page")
    assert response.status_code == 200
    assert response.json() == {"clusters": [], "nextCursor": None}


async def test_browse_picks_nonblank_statement_with_stable_score_tie(browse_postgres):
    client, sessions = browse_postgres
    async with sessions() as db:
        db.add(Cluster(id=50, name=None, summary=None, pipeline_version="v2"))
        for index, (score, statement) in enumerate([
            (None, "Unscored statement"),
            (10, "Chosen statement"),
            (10, "Later ID with the same score"),
            (100, "   "),
        ]):
            db.add(ClusterItem(
                id=str(uuid.UUID(int=5000 + index)),
                cluster_id=50,
                pipeline_version="v2",
                score=score,
                posted_at=OBSERVED,
                problem_statement=statement,
            ))
        await db.commit()
    response = await client.get("/clusters/browse/page")
    assert response.status_code == 200
    row = next(row for row in response.json()["clusters"] if row["id"] == 50)
    assert row["problemStatement"] == "Chosen statement"
    assert row["name"] is None


async def test_browse_preserves_existing_discovery_contracts(browse_postgres):
    client, _ = browse_postgres
    search = await client.post(
        "/clusters/search/query", json={"query": "Cluster", "sort": "newest", "limit": 2}
    )
    browse = await client.get("/clusters/browse", params={"limit": 2})
    for response in [search, browse]:
        assert response.status_code == 200
        assert response.json()["total"] == 7
        assert len(response.json()["data"]) == 2
