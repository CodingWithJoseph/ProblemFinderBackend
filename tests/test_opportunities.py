"""Tests for the /opportunities routes.

Focus: the endpoints return the (post-JOB-3) non-scoring fields and inline
cluster growth fields. The legacy signal/wtp/intensity/sentiment fields were
removed from the API.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from app.routes import opportunities as opp_module
from app.models import PlanEnum, UserFlag
from conftest import (
    FakeResult,
    make_opportunity,
    make_subscription,
    make_cluster,
    make_snapshot,
    opportunity_row,
    embedding_response,
)

# Fields the simplified pipeline still exposes.
KEPT_FIELDS = ["id", "title", "sourceScore", "trending", "numComments"]

# Fields removed in JOB 3 that must NOT appear in responses.
REMOVED_FIELDS = [
    "signalScore", "intensityScore", "sentimentScore", "wtpConfidence",
    "workaroundConfidence", "wtpSignal", "intensity", "sentiment", "domain",
    "signalStrength", "industryCategory",
]


async def test_get_opportunities_returns_core_fields(client, fake_db, auth_headers):
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
    for field in KEPT_FIELDS:
        assert field in item, f"missing field {field}"
    for field in REMOVED_FIELDS:
        assert field not in item, f"removed field {field} still present"
    assert item["sourceScore"] == 120.0
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
    for field in KEPT_FIELDS:
        assert field in item


async def test_get_opportunities_batch_empty_ids(client, fake_db, auth_headers):
    resp = await client.get("/opportunities/batch?ids=", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"data": []}


async def test_search_returns_core_fields(client, fake_db, auth_headers, monkeypatch):
    # Pro plan so the free-tier search-usage gate is skipped.
    sub = make_subscription(plan=PlanEnum.pro)
    # Row carries stale scoring columns; reconstruction must ignore them.
    row = opportunity_row(cluster_id=None, source_score=72.0, signal_score=999.0)
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
    assert item["sourceScore"] == 72.0
    for field in REMOVED_FIELDS:
        assert field not in item


async def test_flag_opportunity_creates_user_flag(client, fake_db, auth_headers):
    opp = make_opportunity()
    pipeline_id = str(uuid.uuid4())
    fake_db.stub(
        execute=[
            FakeResult(rows=[opp]),     # opportunity lookup
            FakeResult(rows=[]),        # no existing flag
        ]
    )

    resp = await client.post(
        f"/opportunities/{opp.id}/flag",
        headers=auth_headers,
        json={"reason": "irrelevant", "note": "Not a real problem", "pipeline_id": pipeline_id},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["opportunityId"] == opp.id
    assert data["pipelineId"] == pipeline_id
    assert data["reason"] == "irrelevant"
    assert data["note"] == "Not a real problem"
    assert data["hidden"] is True
    assert len(fake_db.added) == 1
    assert fake_db.commit_count == 1


async def test_flag_opportunity_updates_existing_flag(client, fake_db, auth_headers):
    opp = make_opportunity()
    existing = UserFlag(
        id=str(uuid.uuid4()),
        user_id="test-uid",
        opportunity_id=opp.id,
        reason="spam",
        note=None,
        hidden=True,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    fake_db.stub(
        execute=[
            FakeResult(rows=[opp]),         # opportunity lookup
            FakeResult(rows=[existing]),    # existing flag
        ]
    )

    resp = await client.post(
        f"/opportunities/{opp.id}/flag",
        headers=auth_headers,
        json={"reason": "duplicate", "note": "Already covered", "hidden": False},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["reason"] == "duplicate"
    assert data["note"] == "Already covered"
    assert data["hidden"] is False
    assert existing.reason == "duplicate"
    assert len(fake_db.added) == 0


async def test_flag_opportunity_not_found(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])  # opportunity lookup
    resp = await client.post(
        "/opportunities/44444444-4444-4444-4444-444444444444/flag",
        headers=auth_headers,
        json={"reason": "irrelevant"},
    )
    assert resp.status_code == 404


async def test_clear_opportunity_flag_deletes_existing_flag(client, fake_db, auth_headers):
    existing = UserFlag(
        id=str(uuid.uuid4()),
        user_id="test-uid",
        opportunity_id="44444444-4444-4444-4444-444444444444",
        reason="irrelevant",
        hidden=True,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    fake_db.stub(execute=[FakeResult(rows=[existing])])

    resp = await client.delete(
        "/opportunities/44444444-4444-4444-4444-444444444444/flag",
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert resp.json() == {"success": True}
    assert fake_db.deleted == [existing]
    assert fake_db.commit_count == 1
