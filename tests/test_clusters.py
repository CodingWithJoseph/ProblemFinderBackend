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


# ──────────────────────────────────────────────────────────────────────────
# BACKEND JOB 2 / 3: cluster-first search + browse
# ──────────────────────────────────────────────────────────────────────────

import uuid as _uuid
from datetime import date as _date
from app.models import SourcePost


def _src_post(**kw):
    defaults = dict(
        id=str(_uuid.uuid4()),
        source="reddit",
        source_post_id="abc",
        source_community_id="r/freelance",
        title="Invoicing is painful",
        body="chasing invoices all week",
        url="https://reddit.com/r/freelance/1",
        score=240,
        num_comments=18,
        posted_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return SourcePost(**defaults)


async def test_cluster_search_returns_clusters_not_posts(client, fake_db, auth_headers):
    cluster = make_cluster(id=42, name="Freelance invoicing pain", status="hot")
    p1 = _src_post(score=240, source_community_id="r/freelance")
    p2 = _src_post(score=90, source_community_id="r/smallbusiness")
    fake_db.stub(
        scalar=[1],  # total
        execute=[
            FakeResult(rows=[cluster]),                # clusters page
            FakeResult(rows=[(42, p1), (42, p2)]),     # posts_by_cluster
            FakeResult(rows=[]),                       # watched lookup (none)
        ],
    )

    resp = await client.get("/clusters/search", params={"q": "invoic"}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["data"][0]
    # Cluster shape, not a post: has name/post_count/sample_posts, no top-level post fields.
    assert item["id"] == 42
    assert item["name"] == "Freelance invoicing pain"
    assert item["post_count"] == 2
    assert item["trending_status"] == "hot"
    assert set(item["subreddits"]) == {"r/freelance", "r/smallbusiness"}
    # sample posts carry the post-level fields
    assert item["sample_posts"][0]["reddit_score"] == 240
    assert item["sample_posts"][0]["subreddit"] == "r/freelance"
    # noise_filter_reason / top_signal were dropped (source_posts has no
    # noise_filter_reason column in the live schema).
    assert "noise_filter_reason" not in item["sample_posts"][0]
    assert "top_signal" not in item
    assert item["is_watched"] is False


async def test_cluster_search_is_watched_reflects_pipeline(client, fake_db, auth_headers):
    cluster = make_cluster(id=42, name="Freelance invoicing pain")
    p1 = _src_post()
    fake_db.stub(
        scalar=[1],
        execute=[
            FakeResult(rows=[cluster]),
            FakeResult(rows=[(42, p1)]),
            FakeResult(rows=[("42",)]),  # pipeline row -> source_cluster_id "42"
        ],
    )
    resp = await client.get("/clusters/search", params={"q": "invoic"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"][0]["is_watched"] is True


async def test_cluster_search_min_length_enforced(client, fake_db, auth_headers):
    resp = await client.get("/clusters/search", params={"q": "a"}, headers=auth_headers)
    assert resp.status_code == 422


async def test_cluster_search_filters_version_and_excludes_singletons(client, fake_db, auth_headers):
    captured = []
    orig_execute = fake_db.execute

    async def _capture(statement, params=None):
        captured.append(str(statement))
        return await orig_execute(statement, params)

    fake_db.execute = _capture
    fake_db.stub(
        scalar=[0],
        execute=[FakeResult(rows=[]), FakeResult(rows=[]), FakeResult(rows=[])],
    )
    resp = await client.get("/clusters/search", params={"q": "invoic"}, headers=auth_headers)
    assert resp.status_code == 200
    main_query = captured[0]
    # singleton clusters excluded + pipeline_version filter applied
    assert "is_singleton" in main_query
    assert "pipeline_version" in main_query


async def test_cluster_browse_sort_modes(client, fake_db, auth_headers):
    for sort_mode, expect in [("largest", "post_count"), ("newest", "last_posted"), ("trending", "growth_rate_wow")]:
        captured = []
        orig_execute = fake_db.execute

        async def _capture(statement, params=None, _c=captured, _o=orig_execute):
            _c.append(str(statement))
            return await _o(statement, params)

        fake_db.execute = _capture
        fake_db.stub(
            scalar=[0],
            execute=[FakeResult(rows=[]), FakeResult(rows=[]), FakeResult(rows=[])],
        )
        resp = await client.get("/clusters/browse", params={"sort": sort_mode}, headers=auth_headers)
        assert resp.status_code == 200, sort_mode
        assert "ORDER BY" in captured[0].upper()
        assert expect in captured[0], (sort_mode, captured[0])
        fake_db.execute = orig_execute


async def test_cluster_browse_subreddit_filter(client, fake_db, auth_headers):
    captured = []
    orig_execute = fake_db.execute

    async def _capture(statement, params=None):
        captured.append(str(statement))
        return await orig_execute(statement, params)

    fake_db.execute = _capture
    fake_db.stub(
        scalar=[0],
        execute=[FakeResult(rows=[]), FakeResult(rows=[]), FakeResult(rows=[])],
    )
    resp = await client.get("/clusters/browse", params={"subreddit": "r/freelance"}, headers=auth_headers)
    assert resp.status_code == 200
    # subreddit filter adds an EXISTS subquery against source_community_id
    assert "EXISTS" in captured[0].upper()
    assert "source_community_id" in captured[0]


async def test_cluster_browse_returns_shape(client, fake_db, auth_headers):
    cluster = make_cluster(id=9, name="AI agents for ops")
    p1 = _src_post(source_community_id="r/SaaS", score=300)
    fake_db.stub(
        scalar=[1],
        execute=[
            FakeResult(rows=[cluster]),
            FakeResult(rows=[(9, p1)]),
            FakeResult(rows=[]),
        ],
    )
    resp = await client.get("/clusters/browse", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["data"][0]["id"] == 9
    assert body["data"][0]["post_count"] == 1
    assert body["data"][0]["subreddits"] == ["r/SaaS"]


async def test_cluster_browse_min_posts_filter(client, fake_db, auth_headers):
    captured = []
    orig_execute = fake_db.execute

    async def _capture(statement, params=None):
        captured.append(str(statement))
        return await orig_execute(statement, params)

    fake_db.execute = _capture
    fake_db.stub(
        scalar=[0],
        execute=[FakeResult(rows=[]), FakeResult(rows=[]), FakeResult(rows=[])],
    )
    resp = await client.get("/clusters/browse", params={"min_posts": 5}, headers=auth_headers)
    assert resp.status_code == 200
    # min_posts applies a post_count >= N predicate on the member-stats subquery
    assert "post_count >=" in captured[0]


async def test_cluster_browse_min_posts_rejects_below_one(client, fake_db, auth_headers):
    resp = await client.get("/clusters/browse", params={"min_posts": 0}, headers=auth_headers)
    assert resp.status_code == 422


async def test_cluster_search_min_posts_filter(client, fake_db, auth_headers):
    captured = []
    orig_execute = fake_db.execute

    async def _capture(statement, params=None):
        captured.append(str(statement))
        return await orig_execute(statement, params)

    fake_db.execute = _capture
    fake_db.stub(
        scalar=[0],
        execute=[FakeResult(rows=[]), FakeResult(rows=[]), FakeResult(rows=[])],
    )
    resp = await client.get(
        "/clusters/search", params={"q": "invoic", "min_posts": 3}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert "post_count >=" in captured[0]


def _capture_main_query(fake_db):
    """Wire a capture wrapper + empty result queue; return the captured list."""
    captured = []
    orig_execute = fake_db.execute

    async def _capture(statement, params=None):
        captured.append(str(statement))
        return await orig_execute(statement, params)

    fake_db.execute = _capture
    fake_db.stub(
        scalar=[0],
        execute=[FakeResult(rows=[]), FakeResult(rows=[]), FakeResult(rows=[])],
    )
    return captured


async def test_cluster_browse_type_and_domain_filters(client, fake_db, auth_headers):
    captured = _capture_main_query(fake_db)
    resp = await client.get(
        "/clusters/browse",
        params={"opportunity_type": "Digital", "opportunity_domain": "Finance and Insurance"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert "opportunity_type =" in captured[0]
    assert "opportunity_domain =" in captured[0]


async def test_cluster_search_type_and_domain_filters(client, fake_db, auth_headers):
    captured = _capture_main_query(fake_db)
    resp = await client.get(
        "/clusters/search",
        params={"q": "invoic", "opportunity_type": "Physical", "opportunity_domain": "Home Services"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert "opportunity_type =" in captured[0]
    assert "opportunity_domain =" in captured[0]


async def test_cluster_browse_all_value_skips_taxonomy_filters(client, fake_db, auth_headers):
    captured = _capture_main_query(fake_db)
    resp = await client.get(
        "/clusters/browse",
        params={"opportunity_type": "All", "opportunity_domain": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    # "All"/empty are treated as "no filter"
    assert "opportunity_type =" not in captured[0]
    assert "opportunity_domain =" not in captured[0]


async def test_cluster_domains_returns_distinct_sorted(client, fake_db, auth_headers):
    # The DB returns distinct opportunity_domain values as 1-tuples; the endpoint
    # flattens them into a plain list under "data".
    fake_db.stub(execute=[FakeResult(rows=[("E-commerce",), ("Fintech",), ("Health",)])])

    resp = await client.get("/clusters/domains", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"data": ["E-commerce", "Fintech", "Health"]}
