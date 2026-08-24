
"""Tests for the release-only cluster and product API surface.

Every route reads the flat ``cluster_items`` table; these tests drive the
FakeSession result queues that each endpoint pops in order.
"""
from app.models import Pipeline, Problem

from conftest import (
    FakeResult,
    make_cluster,
    make_cluster_item,
    make_pipeline,
    make_problem,
)


# â”€â”€ Cluster discovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def test_search_clusters(client, fake_db, auth_headers):
    cluster = make_cluster()
    item = make_cluster_item(cluster_id=cluster.id)
    fake_db.stub(
        scalar=[1],  # total
        execute=[
            FakeResult(rows=[cluster]),   # page of clusters
            FakeResult(rows=[item]),      # _items_by_cluster
            FakeResult(rows=[]),          # _watched_cluster_ids
        ],
    )
    resp = await client.get("/clusters/search?q=invoicing", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    row = body["data"][0]
    assert row["id"] == 42
    assert row["sources"] == ["reddit"]
    assert row["problemStatement"]
    assert row["post_count"] == 1
    assert row["is_watched"] is False


async def test_search_clusters_can_match_item_fields(client, fake_db, auth_headers):
    cluster = make_cluster(name="Generic operations pain")
    item = make_cluster_item(
        cluster_id=cluster.id,
        problem_statement="Property managers cannot track tenant repairs cleanly.",
    )
    fake_db.stub(
        scalar=[1],
        execute=[
            FakeResult(rows=[cluster]),
            FakeResult(rows=[item]),
            FakeResult(rows=[]),
        ],
    )
    resp = await client.get("/clusters/search?q=tenant%20repairs", headers=auth_headers)
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["problemStatement"] == "Property managers cannot track tenant repairs cleanly."


async def test_query_clusters_returns_confirmed_filters_and_pagination(client, fake_db, auth_headers):
    cluster = make_cluster()
    item = make_cluster_item(cluster_id=cluster.id)
    fake_db.stub(
        scalar=[12],
        execute=[
            FakeResult(rows=[cluster]),
            FakeResult(rows=[item]),
            FakeResult(rows=[]),
        ],
    )

    resp = await client.post(
        "/clusters/search/query",
        headers=auth_headers,
        json={
            "query": "invoice pain",
            "sources": ["reddit"],
            "communities": ["r/freelance"],
            "min_posts": 3,
            "observed_after": "2026-05-01T00:00:00Z",
            "sort": "largest",
            "limit": 5,
            "offset": 5,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 12
    assert body["data"][0]["id"] == 42
    assert body["applied_filters"] == {
        "query": "invoice pain",
        "sources": ["reddit"],
        "communities": ["r/freelance"],
        "min_posts": 3,
        "observed_after": "2026-05-01T00:00:00+00:00",
        "sort": "largest",
    }
    assert body["pagination"] == {
        "limit": 5,
        "offset": 5,
        "returned": 1,
        "has_more": True,
        "next_offset": 10,
    }


async def test_query_clusters_rejects_unknown_or_invalid_filters(client, auth_headers):
    unknown = await client.post(
        "/clusters/search/query",
        headers=auth_headers,
        json={"query": "invoice pain", "sql": "drop table clusters"},
    )
    assert unknown.status_code == 422

    invalid = await client.post(
        "/clusters/search/query",
        headers=auth_headers,
        json={"query": "x", "sort": "magic"},
    )
    assert invalid.status_code == 422


async def test_browse_clusters_default_sort(client, fake_db, auth_headers):
    cluster = make_cluster()
    item = make_cluster_item()
    fake_db.stub(
        scalar=[1],
        execute=[FakeResult(rows=[cluster]), FakeResult(rows=[item]), FakeResult(rows=[])],
    )
    resp = await client.get("/clusters/browse", headers=auth_headers)
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["post_count"] == 1


# Cluster detail and evidence

async def test_cluster_detail_problem_breakdown(client, fake_db, auth_headers):
    cluster = make_cluster()
    item = make_cluster_item()
    fake_db.stub(
        scalar=[1],  # total_posts
        execute=[
            FakeResult(rows=[cluster]),       # _fetch_cluster
            FakeResult(rows=[item]),          # top evidence
            FakeResult(rows=[item]),          # page items
            FakeResult(rows=[item]),          # all items for derived metadata
        ],
    )
    resp = await client.get("/clusters/42", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["problemStatement"]
    assert "solutionAngle" not in body
    assert len(body["topEvidence"]) == 1
    assert body["totalPosts"] == 1
    assert body["postIds"] == [item.id]


async def test_cluster_detail_404(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])
    resp = await client.get("/clusters/999", headers=auth_headers)
    assert resp.status_code == 404


async def test_public_demo_routes_are_removed(client):
    assert (await client.get("/public/clusters")).status_code == 404
    assert (await client.get("/public/search?q=test")).status_code == 404


# Problems

async def test_list_problems(client, fake_db, auth_headers):
    problem = make_problem()
    fake_db.stub(execute=[FakeResult(rows=[problem])])
    resp = await client.get(f"/problems?pipeline_id={problem.pipeline_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


async def test_similar_posts_uses_cluster_items(client, fake_db, auth_headers):
    problem = make_problem()
    item = make_cluster_item()
    fake_db.stub(execute=[FakeResult(rows=[problem]), FakeResult(rows=[item])])
    resp = await client.get(f"/problems/{problem.id}/similar-posts", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data[0]["title"] == item.title
    assert data[0]["problemStatement"]


# Project creation lifecycle

async def test_watch_cluster_creates_analyze_case_without_legacy_problems(
    client,
    fake_db,
    auth_headers,
):
    cluster = make_cluster()
    item_a = make_cluster_item(problem_statement="P1", source_item_id="src-1")
    fake_db.stub(
        execute=[
            FakeResult(rows=[]),
            FakeResult(rows=[cluster]),
            FakeResult(rows=[(item_a.id,)]),
        ]
    )

    resp = await client.post("/pipeline/watch", json={"cluster_id": "42"}, headers=auth_headers)

    assert resp.status_code == 200
    assert not any(isinstance(o, Problem) for o in fake_db.added)
    assert any(isinstance(o, Pipeline) for o in fake_db.added)
