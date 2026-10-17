"""Opt-in Discovery and category-pagination checks against isolated local PostgreSQL."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import get_uid
from app.browse_contract import BrowseCategory, BrowseSort
from app.browse_cursor import decode_browse_cursor
from app.config import PIPELINE_VERSION
from app.db import Base, get_db
from app.discovery_contract import DiscoveryCategory
from app.models import Cluster, ClusterItem, Pipeline
from app.routes.clusters import router
from app.services.discovery import load_discovery_dashboard, load_discovery_row
from app.services.temp_discovery_preview import load_discovery_preview_dashboard


DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="Set TEST_DATABASE_URL to isolated local PostgreSQL")
OBSERVED = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def add_cluster(db, cluster_id, category, name, *, summary="A complete summary", version="v2"):
    db.add(Cluster(
        id=cluster_id,
        name=name,
        summary=summary,
        pipeline_version=version,
        discovery_category=category,
    ))


def add_item(db, cluster_id, statement, *, score=10, date=OBSERVED, version="v2"):
    db.add(ClusterItem(
        cluster_id=cluster_id,
        pipeline_version=version,
        problem_statement=statement,
        score=score,
        posted_at=date,
    ))


@pytest_asyncio.fixture
async def discovery_postgres():
    url = urlparse(DSN.replace("postgresql+asyncpg", "postgresql"))
    assert url.hostname in {"127.0.0.1", "localhost"} and url.path == "/auth_migration", (
        "Refusing a non-test database"
    )
    schema = f"discovery_test_{uuid.uuid4().hex}"
    admin = create_async_engine(DSN)
    engine = None
    created = False
    try:
        async with admin.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        engine = create_async_engine(
            DSN,
            connect_args={"server_settings": {"search_path": f"{schema},public"}},
        )
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[Cluster.__table__, ClusterItem.__table__, Pipeline.__table__]
            ))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            add_cluster(db, 30, "build_and_tooling", "Build tooling failure")
            add_cluster(db, 20, "data_and_apis", "Database API failure")
            add_cluster(db, 10, "workflow_and_automation", "Workflow automation failure")
            add_cluster(db, 25, "uncategorized", "Build tooling database API failure")
            add_cluster(db, 40, "uncategorized", "Healthcare billing failure")
            add_item(db, 30, "Build tooling representative", score=20)
            add_item(db, 30, "Another tooling statement", score=5)
            add_item(db, 30, " \t\n", score=100, date=OBSERVED + timedelta(days=1))
            add_item(db, 30, "Old pipeline statement", score=1000,
                     date=OBSERVED + timedelta(days=10), version="v1")
            add_item(db, 20, "Database API representative")
            add_item(db, 20, "Another API statement", score=5)
            add_item(db, 10, "Workflow automation representative", date=None)
            add_item(db, 25, "A shared preview match", date=OBSERVED - timedelta(days=1))
            add_item(db, 40, "Healthcare representative", date=OBSERVED - timedelta(days=2))
            add_cluster(db, 31, "build_and_tooling", " \t\n")
            add_cluster(db, 32, "build_and_tooling", "Build missing summary", summary=" \t\n")
            add_cluster(db, 33, "build_and_tooling", "Build missing statement")
            add_cluster(db, 34, "build_and_tooling", "Build old evidence")
            add_cluster(db, 35, "build_and_tooling", "Build old cluster", version="v1")
            add_cluster(db, 36, "build_and_tooling", "Build no evidence")
            for cluster_id in (31, 32, 35):
                add_item(db, cluster_id, "Build statement")
            add_item(db, 33, None)
            add_item(db, 33, " \t\n")
            add_item(db, 34, "Old build statement", version="v1")
            add_item(db, None, "Unassigned build statement")
            db.add_all([
                Pipeline(user_id="alice", name="Progressed", source_cluster_id="30", stage="analyzing"),
                Pipeline(user_id="alice", name="Duplicate", source_cluster_id="30"),
                Pipeline(user_id="alice", name="Removed", source_cluster_id="20", removed_at=OBSERVED),
                Pipeline(user_id="bob", name="Other user", source_cluster_id="20"),
            ])
            await db.commit()
        app = FastAPI()
        app.include_router(router)

        async def override_db():
            async with sessions() as db:
                yield db

        async def override_uid(request: Request):
            return request.headers.get("X-Test-Uid", "alice")

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_uid] = override_uid
        yield sessions, app
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_discovery_eligibility_statistics_and_statement(discovery_postgres):
    sessions, _ = discovery_postgres
    async with sessions() as db:
        rows = await load_discovery_row(db, limit=100)
    assert [row[0].id for row in rows] == [30, 20, 25, 40, 10]
    _, count, date, statement = rows[0]
    assert count == 3  # Includes the blank current item, excludes the old pipeline item.
    assert date == OBSERVED + timedelta(days=1)
    assert statement == "Build tooling representative"
    assert rows[-1][2] is None


async def test_dashboard_categories_and_account_specific_membership(discovery_postgres):
    sessions, _ = discovery_postgres
    async with sessions() as db:
        alice = await load_discovery_dashboard(db, "alice")
        bob = await load_discovery_dashboard(db, "bob")
    assert alice.featured.id == 30 and alice.featured.isWatched
    assert not bob.featured.isWatched
    assert [group.category for group in alice.collections] == [
        DiscoveryCategory.BUILD_AND_TOOLING,
        DiscoveryCategory.DATA_AND_APIS,
        DiscoveryCategory.WORKFLOW_AND_AUTOMATION,
    ]
    assert [[card.id for card in group.clusters] for group in alice.collections] == [[30], [20], [10]]
    assert alice.collections[0].clusters[0].isWatched
    assert not alice.collections[1].clusters[0].isWatched
    assert bob.collections[1].clusters[0].isWatched


async def test_preview_reuses_search_and_does_not_change_saved_categories(discovery_postgres):
    sessions, _ = discovery_postgres
    async with sessions() as db:
        before = dict((await db.execute(select(Cluster.id, Cluster.discovery_category))).all())
        preview = await load_discovery_preview_dashboard(db, "alice")
        after = dict((await db.execute(select(Cluster.id, Cluster.discovery_category))).all())
    assert before == after
    assert [[card.id for card in group.clusters] for group in preview.collections] == [[30, 25], [20], [10]]
    assert all(card.category == group.category for group in preview.collections for card in group.clusters)
    assert preview.featured.id == 30 and preview.featured.isWatched
    assert before[25] == "uncategorized"


async def test_empty_dashboards(discovery_postgres):
    sessions, _ = discovery_postgres
    async with sessions() as db:
        await db.execute(delete(Pipeline))
        await db.execute(delete(ClusterItem))
        await db.execute(delete(Cluster))
        for loader in (load_discovery_dashboard, load_discovery_preview_dashboard):
            dashboard = await loader(db, "alice")
            assert dashboard.featured is None
            assert len(dashboard.collections) == 3
            assert all(group.clusters == [] for group in dashboard.collections)


async def test_saved_discovery_endpoint_and_missing_auth(discovery_postgres):
    _, app = discovery_postgres
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/clusters/discovery")
        assert response.status_code == 200
        body = response.json()
        assert body["featured"]["id"] == 30
        assert body["featured"]["date"] == "2026-10-18T12:00:00Z"
        assert body["collections"][0]["clusters"][1]["category"] == "build_and_tooling"
        app.dependency_overrides.pop(get_uid)
        assert (await client.get("/clusters/discovery")).status_code == 401


async def test_category_pagination_and_cross_category_cursor(discovery_postgres):
    sessions, app = discovery_postgres
    async with sessions() as db:
        for cluster_id in (21, 22):
            add_cluster(db, cluster_id, "data_and_apis", f"Database API {cluster_id}")
            add_item(db, cluster_id, "API statement")
            add_item(db, cluster_id, "Another API statement", score=5)
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cursor = None
        seen = []
        first_cursor = None
        for expected_id in (22, 21, 20):
            params = {"category": "data_and_apis", "limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.get("/clusters/browse/page", params=params)
            assert response.status_code == 200
            page = response.json()
            assert [card["id"] for card in page["clusters"]] == [expected_id]
            seen.append(expected_id)
            cursor = page["nextCursor"]
            if first_cursor is None:
                first_cursor = cursor
        assert cursor is None and seen == [22, 21, 20]
        decoded = decode_browse_cursor(
            first_cursor, category=BrowseCategory.DATA_AND_APIS,
            sort=BrowseSort.NEWEST, pipeline_version=PIPELINE_VERSION,
        )
        assert decoded.cluster_id == 22
        response = await client.get("/clusters/browse/page", params={
            "category": "build_and_tooling", "cursor": first_cursor,
        })
        assert response.status_code == 422
