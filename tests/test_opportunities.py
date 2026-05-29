"""Tests for the /opportunities routes.

Focus: the endpoints return the enriched signal fields (signalScore,
intensityScore, sentimentScore, wtpConfidence, workaroundConfidence) and inline
cluster growth fields.
"""

from unittest.mock import AsyncMock

from app.routes import opportunities as opp_module
from conftest import (
    FakeResult,
    make_opportunity,
    make_subscription,
    make_cluster,
    make_snapshot,
    opportunity_row,
    embedding_response,
)
from app.models import PlanEnum

SIGNAL_FIELDS = [
    "signalScore",
    "intensityScore",
    "sentimentScore",
    "wtpConfidence",
    "workaroundConfidence",
]


async def test_get_opportunities_returns_signal_fields(client, fake_db, auth_headers):
    opp = make_opportunity(cluster_id=None)
    fake_db.stub(
        execute=[
            FakeResult(rows=[]),       # _get_plan -> no subscription -> free
            FakeResult(rows=[opp]),    # opportunities page
        ],
        scalar=[1],                    # total count
    )

    resp = await client.get("/opportunities", headers=auth_headers)
    assert resp.status_code == 200

    body = resp.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert len(body["data"]) == 1

    item = body["data"][0]
    for field in SIGNAL_FIELDS:
        assert field in item, f"missing signal field {field}"
    assert item["signalScore"] == 87.5
    assert item["intensityScore"] == 0.95
    assert item["sentimentScore"] == -0.9
    assert item["wtpConfidence"] == 0.91
    assert item["workaroundConfidence"] == 0.42
    assert item["wtpSignal"] is True
    assert item["trending"] is True


async def test_get_opportunities_includes_inline_cluster_growth(client, fake_db, auth_headers):
    opp = make_opportunity(cluster_id=42)
    cluster = make_cluster(id=42, growth_rate_wow=0.35, growth_rate_mom=1.2, post_velocity=140.0)
    snapshot = make_snapshot(cluster_id=42, post_count=50)
    fake_db.stub(
        execute=[
            FakeResult(rows=[]),            # _get_plan
            FakeResult(rows=[opp]),         # opportunities page
            FakeResult(rows=[cluster]),     # _build_cluster_map: clusters
            FakeResult(rows=[snapshot]),    # _build_cluster_map: latest snapshots
        ],
        scalar=[1],
    )

    resp = await client.get("/opportunities", headers=auth_headers)
    assert resp.status_code == 200

    cluster_payload = resp.json()["data"][0]["cluster"]
    assert cluster_payload is not None
    assert cluster_payload["growthRateWow"] == 0.35
    assert cluster_payload["growthRateMom"] == 1.2
    assert cluster_payload["postVelocity"] == 140.0
    assert cluster_payload["postCount"] == 50


async def test_get_opportunities_batch(client, fake_db, auth_headers):
    opp = make_opportunity(cluster_id=None)
    fake_db.stub(execute=[FakeResult(rows=[opp])])

    resp = await client.get(f"/opportunities/batch?ids={opp.id}", headers=auth_headers)
    assert resp.status_code == 200

    data = resp.json()["data"]
    assert len(data) == 1
    item = data[0]
    assert item["id"] == opp.id
    for field in SIGNAL_FIELDS:
        assert field in item


async def test_get_opportunities_batch_empty_ids(client, fake_db, auth_headers):
    resp = await client.get("/opportunities/batch?ids=", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


async def test_search_returns_signal_fields(client, fake_db, auth_headers, monkeypatch):
    # Pro plan so the free-tier search-usage gate is skipped.
    sub = make_subscription(plan=PlanEnum.pro)
    row = opportunity_row(cluster_id=None, signal_score=72.0, intensity_score=0.67)
    fake_db.stub(
        execute=[
            FakeResult(rows=[sub]),         # _get_plan
            FakeResult(mappings=[row]),     # hybrid_search results
            FakeResult(rows=[]),            # cluster name search -> none
        ],
        scalar=[1],                         # hybrid_search total count
    )

    monkeypatch.setattr(
        opp_module.openai_client.embeddings,
        "create",
        AsyncMock(return_value=embedding_response([0.01] * 1536)),
    )

    resp = await client.get("/opportunities/search?q=invoicing", headers=auth_headers)
    assert resp.status_code == 200

    body = resp.json()
    assert body["total"] == 1
    assert body["clusters"] == []
    item = body["data"][0]
    for field in SIGNAL_FIELDS:
        assert field in item
    assert item["signalScore"] == 72.0
    assert item["intensityScore"] == 0.67
