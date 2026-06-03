"""Tests for the public, unauthenticated read-only endpoints (BACKEND JOB 2)."""

import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from app.db import get_db
from app.models import Cluster, SourcePost
from app.routes.public import router as public_router
from conftest import FakeResult, make_cluster


def _source_post(**kw):
    defaults = dict(
        id=str(uuid.uuid4()),
        source="reddit",
        source_post_id="abc",
        source_community_id="r/freelance",
        title="Invoicing is painful",
        body="chasing invoices all week",
        url="https://reddit.com/r/freelance/1",
        score=240,
        num_comments=18,
    )
    defaults.update(kw)
    return SourcePost(**defaults)


@pytest_asyncio.fixture
async def public_client(fake_db):
    application = FastAPI()
    application.include_router(public_router)

    async def _get_db_override():
        yield fake_db

    application.dependency_overrides[get_db] = _get_db_override
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_public_clusters_list_shape(public_client, fake_db):
    cluster = make_cluster(id=42, is_singleton=False, status="hot")
    p1 = _source_post(score=240, source_community_id="r/freelance")
    p2 = _source_post(score=90, source_community_id="r/smallbusiness")
    fake_db.stub(execute=[
        FakeResult(rows=[cluster]),                 # clusters query
        FakeResult(rows=[(42, p1), (42, p2)]),      # _posts_by_cluster
    ])

    resp = await public_client.get("/public/clusters")
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"

    data = resp.json()["data"]
    assert len(data) == 1
    c = data[0]
    assert c["id"] == 42
    assert c["post_count"] == 2
    assert c["trending_status"] == "hot"
    assert c["is_singleton"] is False
    assert set(c["subreddits"]) == {"r/freelance", "r/smallbusiness"}
    # sample_posts ordered by reddit_score desc, capped at 3
    assert c["sample_posts"][0]["reddit_score"] == 240
    assert c["sample_posts"][0]["subreddit"] == "r/freelance"


async def test_public_cluster_detail_includes_all_posts(public_client, fake_db):
    cluster = make_cluster(id=7)
    p1 = _source_post(score=50)
    fake_db.stub(execute=[
        FakeResult(rows=[cluster]),         # cluster lookup
        FakeResult(rows=[(7, p1)]),         # _posts_by_cluster
    ])

    resp = await public_client.get("/public/clusters/7")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["id"] == 7
    assert len(body["posts"]) == 1
    assert body["posts"][0]["reddit_score"] == 50


async def test_public_cluster_detail_404(public_client, fake_db):
    fake_db.stub(execute=[FakeResult(rows=[])])
    resp = await public_client.get("/public/clusters/999")
    assert resp.status_code == 404


async def test_public_search_returns_clusters(public_client, fake_db):
    cluster = make_cluster(id=42, name="Freelance invoicing pain")
    fake_db.stub(execute=[
        FakeResult(rows=[cluster]),     # ilike cluster search
        FakeResult(rows=[]),            # _posts_by_cluster (no members)
    ])
    resp = await public_client.get("/public/search", params={"q": "invoic"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data[0]["id"] == 42
    assert data[0]["post_count"] == 0


async def test_public_stats_shape(public_client, fake_db):
    fake_db.stub(scalar=[18320, 11234, 324])
    resp = await public_client.get("/public/stats")
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    body = resp.json()
    assert body["conversationsAnalyzed"] == 18320
    assert body["clustersDetected"] == 324
