
"""Tests for the simplified 6-table (cluster_items) API surface.

Every route reads the flat ``cluster_items`` table; these tests drive the
FakeSession result queues that each endpoint pops in order.
"""
from app.models import Pipeline, Problem

from conftest import (
    FakeResult,
    make_cluster,
    make_cluster_signal,
    make_cluster_item,
    make_snapshot,
    make_pipeline,
    make_problem,
)
from sqlalchemy.exc import SQLAlchemyError


# â”€â”€ Cluster discovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def test_search_clusters(client, fake_db, auth_headers):
    cluster = make_cluster()
    signal = make_cluster_signal(signal_score=0.91)
    item = make_cluster_item(cluster_id=cluster.id)
    fake_db.stub(
        scalar=[1],  # total
        execute=[
            FakeResult(rows=[cluster]),   # page of clusters
            FakeResult(rows=[item]),      # _items_by_cluster
            FakeResult(rows=[]),          # _watched_cluster_ids
            FakeResult(rows=[signal]),    # _signals_by_cluster
        ],
    )
    resp = await client.get(
        "/clusters/search?q=invoicing&opportunity_type=software",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    row = body["data"][0]
    assert row["id"] == 42
    assert row["signalScore"] == 0.91
    assert row["opportunity_domain"] == "fintech"
    assert row["sources"] == ["reddit"]
    assert row["problemStatement"]
    assert row["post_count"] == 1
    assert row["is_watched"] is False


async def test_search_clusters_can_match_item_fields(client, fake_db, auth_headers):
    cluster = make_cluster(name="Generic operations pain")
    item = make_cluster_item(
        cluster_id=cluster.id,
        opportunity_domain="real estate",
        problem_statement="Property managers cannot track tenant repairs cleanly.",
    )
    fake_db.stub(
        scalar=[1],
        execute=[
            FakeResult(rows=[cluster]),
            FakeResult(rows=[item]),
            FakeResult(rows=[]),
            FakeResult(rows=[]),
        ],
    )
    resp = await client.get("/clusters/search?q=tenant%20repairs", headers=auth_headers)
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["opportunity_domain"] == "real estate"
    assert row["problemStatement"] == "Property managers cannot track tenant repairs cleanly."


async def test_query_clusters_returns_confirmed_filters_and_pagination(client, fake_db, auth_headers):
    cluster = make_cluster()
    signal = make_cluster_signal(signal_score=0.91)
    item = make_cluster_item(cluster_id=cluster.id)
    fake_db.stub(
        scalar=[12],
        execute=[
            FakeResult(rows=[cluster]),
            FakeResult(rows=[item]),
            FakeResult(rows=[]),
            FakeResult(rows=[signal]),
        ],
    )

    resp = await client.post(
        "/clusters/search/query",
        headers=auth_headers,
        json={
            "query": "invoice pain",
            "opportunity_domains": ["Fintech", "fintech", "all"],
            "opportunity_types": ["software"],
            "sources": ["reddit"],
            "communities": ["r/freelance"],
            "min_posts": 3,
            "observed_after": "2026-05-01T00:00:00Z",
            "trending_only": True,
            "min_signal_score": 0.7,
            "sort": "signal_score",
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
        "opportunity_domains": ["Fintech"],
        "opportunity_types": ["software"],
        "sources": ["reddit"],
        "communities": ["r/freelance"],
        "min_posts": 3,
        "observed_after": "2026-05-01T00:00:00+00:00",
        "trending_only": True,
        "min_signal_score": 0.7,
        "sort": "signal_score",
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
    cluster = make_cluster(trending=False)
    item = make_cluster_item()
    fake_db.stub(
        scalar=[1],
        execute=[FakeResult(rows=[cluster]), FakeResult(rows=[item]), FakeResult(rows=[]), FakeResult(rows=[])],
    )
    resp = await client.get("/clusters/browse", headers=auth_headers)
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["signalScore"] == 0.82
    assert row["trending_status"] is None


async def test_browse_clusters_survives_unavailable_cluster_signals(client, fake_db, auth_headers):
    cluster = make_cluster(signal_score=0.72, trending=False)
    item = make_cluster_item()
    fake_db.stub(
        scalar=[1],
        execute=[
            FakeResult(rows=[cluster]),
            FakeResult(rows=[item]),
            FakeResult(rows=[]),
            SQLAlchemyError("cluster_signals missing"),
        ],
    )
    resp = await client.get("/clusters/browse?limit=20&offset=0&sort=newest", headers=auth_headers)
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["signalScore"] == 0.72
    assert row["trending_status"] is None


async def test_browse_clusters_trending_sort(client, fake_db, auth_headers):
    cluster = make_cluster(trending=True)
    item = make_cluster_item()
    fake_db.stub(
        scalar=[1],
        execute=[FakeResult(rows=[cluster]), FakeResult(rows=[item]), FakeResult(rows=[]), FakeResult(rows=[])],
    )
    resp = await client.get("/clusters/browse?sort=trending", headers=auth_headers)
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["trending_status"] == "trending"


async def test_trending_accessible_to_free(client, fake_db, auth_headers):
    # Tier gating has been removed: trending is available to every user.
    cluster = make_cluster()
    signal = make_cluster_signal(signal_score=0.93)
    fake_db.stub(execute=[
        FakeResult(rows=[(cluster, 50, signal)]),
    ])
    resp = await client.get("/clusters/trending", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"][0]["signalScore"] == 0.93


async def test_trending_orders_by_signal_score(client, fake_db, auth_headers):
    cluster = make_cluster()
    signal = make_cluster_signal(signal_score=0.94)
    fake_db.stub(execute=[
        FakeResult(rows=[(cluster, 50, signal)]),
    ])
    resp = await client.get("/clusters/trending", headers=auth_headers)
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["signalScore"] == 0.94
    assert row["postCount"] == 50


# Cluster detail and evidence

async def test_cluster_detail_problem_breakdown(client, fake_db, auth_headers):
    cluster = make_cluster()
    signal = make_cluster_signal(signal_score=0.9)
    item = make_cluster_item()
    fake_db.stub(
        scalar=[1],  # total_posts
        execute=[
            FakeResult(rows=[cluster]),       # _fetch_cluster
            FakeResult(rows=[item]),          # top evidence
            FakeResult(rows=[item]),          # page items
            FakeResult(rows=[signal]),        # cluster signal
            FakeResult(rows=[(item.id,)]),    # all ids
        ],
    )
    resp = await client.get("/clusters/42", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["problemStatement"]
    assert body["solutionAngle"]
    assert len(body["topEvidence"]) == 1
    assert body["totalPosts"] == 1
    assert body["postIds"] == [item.id]


async def test_cluster_detail_404(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])
    resp = await client.get("/clusters/999", headers=auth_headers)
    assert resp.status_code == 404


async def test_cluster_snapshots(client, fake_db, auth_headers):
    cluster = make_cluster()
    signal = make_cluster_signal(signal_score=0.95)
    snap = make_snapshot()
    fake_db.stub(execute=[FakeResult(rows=[cluster]), FakeResult(rows=[snap]), FakeResult(rows=[signal])])
    resp = await client.get("/clusters/42/snapshots", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["signalScore"] == 0.95
    assert len(body["snapshots"]) == 1


# Public marketing API

async def test_public_clusters(client, fake_db):
    cluster = make_cluster()
    item = make_cluster_item()
    fake_db.stub(execute=[FakeResult(rows=[cluster]), FakeResult(rows=[item])])
    resp = await client.get("/public/clusters")
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["signal_score"] == 0.82
    assert row["post_count"] == 1
    assert row["subreddits"] == ["r/freelance"]


async def test_public_stats_counts_items_and_clusters(client, fake_db):
    fake_db.stub(scalar=[18320, 324])
    resp = await client.get("/public/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversationsAnalyzed"] == 18320
    assert body["clustersDetected"] == 324
    assert body["opportunitiesFound"] == 18320


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


# â”€â”€ Watch-cluster Breakdown seeding â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def test_watch_cluster_seeds_problems(client, fake_db, auth_headers):
    cluster = make_cluster()
    item_a = make_cluster_item(problem_statement="P1", solution_angle="S1", source_item_id="src-1", score=100)
    item_b = make_cluster_item(problem_statement="P2", solution_angle=None, source_item_id="src-2", score=80)
    item_skip = make_cluster_item(problem_statement=None, solution_angle="S3", source_item_id="src-3", score=70)
    item_dup = make_cluster_item(problem_statement="P1", solution_angle="S1-dup", source_item_id="src-1b", score=50)
    fake_db.stub(
        execute=[
            FakeResult(rows=[]),                              # _find_watch_card (none)
            FakeResult(rows=[cluster]),                       # cluster lookup
            FakeResult(rows=[(item_a.id,)]),                  # post_id_rows
            FakeResult(rows=[item_a, item_b, item_skip, item_dup]),  # load_cluster_items (seed)
        ],
        scalar=[0],  # existing problems count == 0 -> seed
    )
    resp = await client.post("/pipeline/watch", json={"cluster_id": "42"}, headers=auth_headers)
    assert resp.status_code == 200

    problems = [o for o in fake_db.added if isinstance(o, Problem)]
    card = next(o for o in fake_db.added if isinstance(o, Pipeline))

    # Deduped by problem_statement (P1 once), null problem_statement skipped, capped.
    assert [p.title for p in problems] == ["P1", "P2"]
    assert [p.position for p in problems] == [0, 1]
    assert [p.source_post_id for p in problems] == ["src-1", "src-2"]
    assert all(p.pipeline_id == card.id and p.user_id == "test-uid" for p in problems)


async def test_watch_cluster_does_not_seed_when_problems_exist(client, fake_db, auth_headers):
    cluster = make_cluster()
    item_a = make_cluster_item(problem_statement="P1", solution_angle="S1")
    fake_db.stub(
        execute=[
            FakeResult(rows=[]),                  # _find_watch_card
            FakeResult(rows=[cluster]),           # cluster lookup
            FakeResult(rows=[(item_a.id,)]),      # post_id_rows
        ],
        scalar=[3],  # pipeline already has problems -> no seeding (no items load)
    )
    resp = await client.post("/pipeline/watch", json={"cluster_id": "42"}, headers=auth_headers)
    assert resp.status_code == 200
    assert not any(isinstance(o, Problem) for o in fake_db.added)
