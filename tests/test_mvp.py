"""Tests for the simplified 6-table (cluster_items) API surface.

Every route reads the flat ``cluster_items`` table; these tests drive the
FakeSession result queues that each endpoint pops in order.
"""
from datetime import datetime, timezone

from app.models import Pipeline, Problem, Task

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


# ── Cluster discovery ───────────────────────────────────────────────────────

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


async def test_cluster_domains_from_items(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[("fintech",), ("devtools",)])])
    resp = await client.get("/clusters/domains", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"] == ["fintech", "devtools"]


# ── Cluster detail / evidence / tasks (MVP views 3, 4, 5) ───────────────────

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


async def test_cluster_items_evidence_list(client, fake_db, auth_headers):
    cluster = make_cluster()
    item = make_cluster_item()
    fake_db.stub(scalar=[1], execute=[FakeResult(rows=[cluster]), FakeResult(rows=[item])])
    resp = await client.get("/clusters/42/items", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    ev = body["data"][0]
    assert ev["problemStatement"]
    assert ev["solutionAngle"]
    assert ev["upvoteRatio"] == 0.95          # recovered from raw_json
    assert ev["topComments"]


async def test_cluster_tasks_distinct_solution_angles(client, fake_db, auth_headers):
    cluster = make_cluster()
    items = [
        make_cluster_item(score=120, solution_angle="Automate invoice reminders."),
        make_cluster_item(score=80, solution_angle="Automate invoice reminders."),  # dup
        make_cluster_item(score=50, solution_angle="Offer escrow for freelancers."),
    ]
    fake_db.stub(execute=[FakeResult(rows=[cluster]), FakeResult(rows=items)])
    resp = await client.get("/clusters/42/tasks", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2  # de-duplicated
    assert data[0]["solutionAngle"] == "Automate invoice reminders."
    assert data[0]["evidenceCount"] == 2


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


# ── Dashboard signals (MVP view 1) ──────────────────────────────────────────

async def test_dashboard_signals(client, fake_db, auth_headers):
    cluster = make_cluster()
    signal = make_cluster_signal(signal_score=0.96)
    fake_db.stub(execute=[
        FakeResult(rows=[(cluster, signal)]),       # top clusters
        FakeResult(rows=[(42, 50)]),                # per-cluster post counts
        FakeResult(rows=[("fintech", 50, 1)]),      # domain breakdown
    ])
    resp = await client.get("/dashboard/signals", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["clusters"][0]["signalScore"] == 0.96
    assert body["clusters"][0]["postCount"] == 50
    assert body["domainBreakdown"][0]["domain"] == "fintech"
    assert body["domainBreakdown"][0]["clusterCount"] == 1


async def test_dashboard_signals_falls_back_to_legacy_cluster_score(client, fake_db, auth_headers):
    cluster = make_cluster(signal_score=0.77)
    fake_db.stub(execute=[
        FakeResult(rows=[(cluster, None)]),
        FakeResult(rows=[(42, 50)]),
        FakeResult(rows=[]),
    ])
    resp = await client.get("/dashboard/signals", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["clusters"][0]["signalScore"] == 0.77


# ── Public + homepage ───────────────────────────────────────────────────────

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


async def test_homepage_stats_alias(client, fake_db):
    fake_db.stub(scalar=[100, 5])
    resp = await client.get("/homepage/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversationsAnalyzed"] == 100
    assert body["clustersDetected"] == 5


# ── Pipeline-anchored Signal / Evidence / brief (MVP view 2) ────────────────

async def test_pipeline_signal_returns_real_cluster_signal(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    signal = make_cluster_signal()
    fake_db.stub(execute=[
        FakeResult(rows=[card]),      # active card
        FakeResult(rows=[signal]),    # cluster_signals lookup
    ])
    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    # Real cluster_signals fields are surfaced flat (no nested envelope/fallback).
    assert body["clusterId"] == 42
    assert body["signalScore"] == 0.91
    assert body["recency"] == 0.88
    assert body["momentum7d"] == 0.2
    assert body["totalPosts"] == 12
    assert body["authorCount"] == 8
    assert body["platformCount"] == 1
    assert body["sourceCommunities"] == ["r/freelance", "r/bookkeeping"]
    assert body["postVolumeByWeek"] == [
        {"week": "2026-W20", "count": 4},
        {"week": "2026-W21", "count": 7},
    ]
    assert body["status"] == "ready"  # surfaced verbatim from the DB enum
    assert body["generatedAt"] == "2026-05-21T00:00:00+00:00"
    # signal_score 0.91 >= 0.6 and momentum_7d 0.2 > 0 -> active.
    assert body["mode"] == "active"
    # 7 of 8 completeness fields are non-null (momentum_90d is None).
    assert body["completeness"] == 0.875
    # Only the problem_statement text is exposed; post_id is stripped.
    assert body["topProblemStatements"] == [
        {"problem_statement": "Freelancers waste hours chasing invoices."}
    ]
    assert "lastError" not in body
    assert "inputFingerprint" not in body


async def test_pipeline_signal_mode_dormant(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    signal = make_cluster_signal(signal_score=0.2)
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[signal])])
    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "dormant"


async def test_pipeline_signal_mode_forming(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    signal = make_cluster_signal(signal_score=0.45, momentum_7d=None)
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[signal])])
    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "forming"


async def test_pipeline_signal_surfaces_every_enum_status_verbatim(client, fake_db, auth_headers):
    # API status set == Supabase cluster_signal_status enum, passed through as-is.
    for status in ["pending", "processing", "ready", "stale", "failed"]:
        card = make_pipeline(id=f"pipe-{status}", source_cluster_id="42")
        signal = make_cluster_signal(status=status)
        fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[signal])])
        resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == status


async def test_pipeline_signal_unknown_status_falls_back_to_failed(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    signal = make_cluster_signal(status="bogus")
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[signal])])
    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"


async def test_pipeline_signal_serves_launched_card(client, fake_db, auth_headers):
    # A launched (but not removed) card keeps its published signal.
    card = make_pipeline(source_cluster_id="42", launched_at=datetime(2026, 6, 17, tzinfo=timezone.utc))
    signal = make_cluster_signal()
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[signal])])
    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["clusterId"] == 42
    assert resp.json()["status"] == "ready"


async def test_pipeline_signal_404_when_card_missing(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    fake_db.stub(execute=[FakeResult(rows=[])])  # no card (or removed)
    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 404


async def test_pipeline_signal_404_when_card_has_no_cluster(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id=None)
    fake_db.stub(execute=[FakeResult(rows=[card])])  # card found, but no source cluster
    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 404


async def test_pipeline_signal_404_when_no_signal_row(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[])])  # no cluster_signals row
    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 404


async def test_pipeline_signal_503_when_cluster_signals_unavailable(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    fake_db.stub(execute=[
        FakeResult(rows=[card]),
        SQLAlchemyError("cluster_signals read failed"),  # DB failure -> fail loudly
    ])
    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 503


async def test_pipeline_signal_evidence(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    item = make_cluster_item()
    fake_db.stub(
        scalar=[1],
        execute=[
            FakeResult(rows=[card]),                # active card
            FakeResult(rows=[item]),                # evidence page
            FakeResult(rows=[("r/freelance",)]),    # communities
        ],
    )
    resp = await client.get(f"/pipeline/{card.id}/signal/evidence", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["communities"] == ["r/freelance"]
    assert body["data"][0]["problemStatement"]


async def test_pipeline_posts_alias(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    item = make_cluster_item()
    fake_db.stub(
        scalar=[1],
        execute=[
            FakeResult(rows=[card]),                # active card
            FakeResult(rows=[item]),                # posts page
            FakeResult(rows=[("r/freelance",)]),    # communities
        ],
    )
    resp = await client.get(f"/pipeline/{card.id}/posts", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["communities"] == ["r/freelance"]
    assert body["data"][0]["title"] == item.title


async def test_pipeline_posts_serves_launched_card(client, fake_db, auth_headers):
    # A launched (but not removed) card keeps its source posts (regression: the
    # Posts/Source-Review view used to 404 on launched cards).
    card = make_pipeline(source_cluster_id="42", launched_at=datetime(2026, 6, 17, tzinfo=timezone.utc))
    item = make_cluster_item()
    fake_db.stub(
        scalar=[1],
        execute=[
            FakeResult(rows=[card]),                # card (active or launched)
            FakeResult(rows=[item]),                # posts page
            FakeResult(rows=[("r/freelance",)]),    # communities
        ],
    )
    resp = await client.get(f"/pipeline/{card.id}/posts", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"][0]["title"] == item.title


async def test_pipeline_signal_brief(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    item = make_cluster_item()
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[item])])
    resp = await client.get(f"/pipeline/{card.id}/signal/brief", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["problemStatement"]
    assert len(body["solutionAngles"]) == 1


# ── Problems (kept feature; similar-posts rewired to cluster_items) ──────────

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


# ── Watch-cluster Breakdown/Task seeding ────────────────────────────────────

async def test_watch_cluster_seeds_problems_and_tasks(client, fake_db, auth_headers):
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
    tasks = [o for o in fake_db.added if isinstance(o, Task)]
    card = next(o for o in fake_db.added if isinstance(o, Pipeline))

    # Deduped by problem_statement (P1 once), null problem_statement skipped, capped.
    assert [p.title for p in problems] == ["P1", "P2"]
    assert [p.position for p in problems] == [0, 1]
    assert [p.source_post_id for p in problems] == ["src-1", "src-2"]
    assert all(p.pipeline_id == card.id and p.user_id == "test-uid" for p in problems)

    # Only P1 had a solution_angle -> one task, linked to the P1 problem.
    assert len(tasks) == 1
    assert tasks[0].title == "S1"
    assert tasks[0].status == "todo"
    assert tasks[0].position == 0
    assert tasks[0].problem_id == problems[0].id
    assert tasks[0].pipeline_id == card.id


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
    assert not any(isinstance(o, (Problem, Task)) for o in fake_db.added)
