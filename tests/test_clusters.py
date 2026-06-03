"""Tests for the /clusters routes.

Focus: cluster endpoints surface the growth fields (growthRateWow,
growthRateMom, postVelocity), on-read signal aggregations, and the Task 3
Opportunity Brief endpoints.
"""

from datetime import datetime, timezone

import app.routes.clusters as clusters_route
from app.models import PlanEnum
from conftest import (
    FakeResult,
    make_cluster,
    make_snapshot,
    make_opportunity,
    make_subscription,
)

GROWTH_FIELDS = ["growthRateWow", "growthRateMom", "postVelocity"]


async def test_trending_clusters_returns_growth_fields(client, fake_db, auth_headers):
    cluster = make_cluster(id=7, growth_rate_wow=0.5, growth_rate_mom=1.1, post_velocity=99.0)
    snapshot = make_snapshot(cluster_id=7, post_count=33)
    fake_db.stub(
        execute=[
            FakeResult(rows=[make_subscription(plan=PlanEnum.pro)]),  # plan lookup (AI-cluster gate)
            FakeResult(rows=[cluster]),    # trending clusters
            FakeResult(rows=[snapshot]),   # latest snapshots
        ]
    )

    resp = await client.get("/clusters/trending", headers=auth_headers)
    assert resp.status_code == 200

    data = resp.json()["data"]
    assert len(data) == 1
    item = data[0]
    for field in GROWTH_FIELDS:
        assert field in item
    assert item["growthRateWow"] == 0.5
    assert item["growthRateMom"] == 1.1
    assert item["postVelocity"] == 99.0
    assert item["postCount"] == 33


async def test_trending_clusters_blocked_for_free_plan(client, fake_db, auth_headers):
    # AI-detected clusters are Pro-only; free users get the limit_reached payload.
    fake_db.stub(execute=[FakeResult(rows=[make_subscription(plan=PlanEnum.free)])])

    resp = await client.get("/clusters/trending", headers=auth_headers)
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "limit_reached"
    assert body["limit"] == "clusters"
    assert body["plan"] == "free"
    assert body["upgrade_required"] == "pro"


async def test_get_cluster_returns_growth_and_signals(client, fake_db, auth_headers):
    cluster = make_cluster(id=42, growth_rate_wow=0.35, growth_rate_mom=1.2, post_velocity=140.0)
    # _get_signal_aggregations is now a single post-count query (JOB 3).
    agg_row = {"total": 10}
    opp = make_opportunity(cluster_id=42)
    fake_db.stub(
        execute=[
            FakeResult(rows=[cluster]),          # cluster lookup
            FakeResult(mappings=[agg_row]),      # signal aggregation (count only)
            FakeResult(rows=[50]),               # latest post count
            FakeResult(rows=[opp]),              # paginated posts
            FakeResult(rows=[(opp.id,)]),        # all post ids
        ],
        scalar=[1],                              # total posts count
    )

    resp = await client.get("/clusters/42", headers=auth_headers)
    assert resp.status_code == 200

    body = resp.json()
    assert body["id"] == 42
    for field in GROWTH_FIELDS:
        assert field in body
    assert body["growthRateMom"] == 1.2
    assert body["postVelocity"] == 140.0

    signals = body["signals"]
    assert signals["totalPosts"] == 10
    # Scoring distributions removed in JOB 3.
    assert "wtpRate" not in signals
    assert "topDomain" not in signals

    assert body["postIds"] == [str(opp.id)]
    assert len(body["posts"]) == 1
    post = body["posts"][0]
    assert post["sourceScore"] == opp.source_score
    assert "signalScore" not in post
    assert "intensityScore" not in post


async def test_get_cluster_not_found(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])  # cluster lookup -> none
    resp = await client.get("/clusters/999", headers=auth_headers)
    assert resp.status_code == 404


async def test_get_cluster_brief_returns_stored_brief(client, fake_db, auth_headers):
    brief = {
        "id": "brief-1",
        "clusterId": "42",
        "title": "Freelance invoicing pain",
        "generatedAt": "2026-05-31T12:00:00+00:00",
    }
    cluster = make_cluster(
        id=42,
        opportunity_brief=brief,
        brief_generated_at=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        brief_model_version="claude-sonnet-4-6",
    )
    fake_db.stub(execute=[FakeResult(rows=[cluster])])

    resp = await client.get("/clusters/42/brief", headers=auth_headers)
    assert resp.status_code == 200

    body = resp.json()
    assert body["clusterId"] == 42
    assert body["brief"] == brief
    assert body["generatedAt"] == "2026-05-31T12:00:00+00:00"
    assert body["modelVersion"] == "claude-sonnet-4-6"


async def test_get_cluster_brief_handles_missing_brief(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[make_cluster(id=42)])])

    resp = await client.get("/clusters/42/brief", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "clusterId": 42,
        "brief": None,
        "generatedAt": None,
        "modelVersion": None,
    }


async def test_refresh_cluster_brief_blocks_free_users(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[make_subscription(plan=PlanEnum.free)])])

    resp = await client.post("/clusters/42/brief/refresh", headers=auth_headers)
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "limit_reached"
    assert body["limit"] == "opportunity_brief"
    assert body["plan"] == "free"
    assert fake_db.commit_count == 0


async def test_refresh_cluster_brief_allows_pro_and_stores_json(
    client, fake_db, auth_headers, monkeypatch
):
    content = {
        "title": "Freelance invoicing pain",
        "oneLineSummary": "Freelancers waste time chasing invoices.",
        "problemSummary": "Late payments create manual follow-up work.",
        "audience": {"primaryUser": "Freelancers", "buyerType": "prosumer", "estimatedMarketSize": "mid"},
        "topSignals": ["Late payment complaints"],
        "workarounds": ["Spreadsheets"],
        "currentAlternatives": ["QuickBooks"],
        "productAngles": [{"title": "Auto chaser", "description": "Automate reminders"}],
        "risks": ["Crowded market"],
        "validationQuestions": ["Will users pay monthly?"],
        "recommendedNextStep": "Interview 10 freelancers.",
        "momentum": "steady",
    }
    captured = {}

    async def fake_generate(context: str):
        captured["context"] = context
        return content

    monkeypatch.setattr(clusters_route, "generate_opportunity_brief", fake_generate)
    monkeypatch.setattr(clusters_route, "brief_model_version", lambda: "claude-test")

    cluster = make_cluster(
        id=42,
        growth_rate_wow=0.0,
        growth_acceleration=0.0,
    )
    post = make_opportunity(cluster_id=42, source_score=87.5)
    snapshot = make_snapshot(id=99, cluster_id=42)
    fake_db.stub(
        execute=[
            FakeResult(rows=[make_subscription(plan=PlanEnum.pro)]),
            FakeResult(rows=[cluster]),
            FakeResult(rows=[post]),
            FakeResult(rows=[snapshot]),
        ]
    )

    resp = await client.post("/clusters/42/brief/refresh", headers=auth_headers)
    assert resp.status_code == 200

    body = resp.json()
    stored = body["brief"]
    assert stored["clusterId"] == "42"
    assert stored["snapshotId"] == "99"
    assert stored["generatedBy"] == "claude-test"
    assert stored["title"] == "Freelance invoicing pain"
    assert stored["evidence"]["totalPostsAnalyzed"] == 1
    assert stored["evidence"]["representativePosts"][0]["score"] == 87.5
    # signalStrength is now reduced to frequency (no pain/urgency/overall scores).
    assert stored["signalStrength"]["frequency"] == 1
    assert "overallScore" not in stored["signalStrength"]
    assert body["modelVersion"] == "claude-test"
    assert cluster.opportunity_brief == stored
    assert cluster.brief_model_version == "claude-test"
    assert cluster.brief_generated_at is not None
    assert fake_db.commit_count == 1
    assert "Cluster name: Freelance invoicing pain" in captured["context"]
