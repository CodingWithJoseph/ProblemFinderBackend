"""Tests for the /clusters routes.

Focus: cluster endpoints surface the growth fields (growthRateWow,
growthRateMom, postVelocity) and on-read signal aggregations.
"""

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

    data = resp.json()["data"]
    assert len(data) == 1
    item = data[0]
    for field in GROWTH_FIELDS:
        assert field in item
    assert item["growthRateWow"] == 0.5
    assert item["growthRateMom"] == 1.1
    assert item["postVelocity"] == 99.0
    assert item["postCount"] == 33


async def test_get_cluster_returns_growth_and_signals(client, fake_db, auth_headers):
    cluster = make_cluster(id=42, growth_rate_wow=0.35, growth_rate_mom=1.2, post_velocity=140.0)
    agg_row = {
        "total": 10,
        "wtp_count": 6,
        "workaround_count": 3,
        "intensity_low": 2,
        "intensity_medium": 3,
        "intensity_high": 5,
        "sentiment_positive": 1,
        "sentiment_negative": 7,
        "sentiment_neutral": 2,
    }
    domain_row = {"domain": "developer tools", "n": 8}
    opp = make_opportunity(cluster_id=42)
    fake_db.stub(
        execute=[
            FakeResult(rows=[cluster]),          # cluster lookup
            FakeResult(mappings=[agg_row]),      # signal aggregation
            FakeResult(mappings=[domain_row]),   # top domain
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
    assert signals["wtpRate"] == 60.0           # 6/10
    assert signals["workaroundRate"] == 30.0    # 3/10
    assert signals["intensity"]["high"] == 50.0
    assert signals["topDomain"] == "developer tools"

    assert body["postIds"] == [str(opp.id)]
    assert len(body["posts"]) == 1
    post = body["posts"][0]
    assert post["signalScore"] == opp.signal_score
    assert post["intensityScore"] == opp.intensity_score
    assert post["wtpConfidence"] == opp.wtp_confidence


async def test_get_cluster_not_found(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])  # cluster lookup -> none
    resp = await client.get("/clusters/999", headers=auth_headers)
    assert resp.status_code == 404
