"""Tests for public homepage stats."""


async def test_homepage_stats_returns_public_aggregate_counts(client, fake_db):
    # Stats now query 3 scalars: source_post_count, opportunity_count, cluster_count.
    fake_db.stub(scalar=[18320, 11234, 324])

    resp = await client.get("/homepage/stats")
    assert resp.status_code == 200

    body = resp.json()
    assert body["conversationsAnalyzed"] == 18320
    assert body["clustersDetected"] == 324
    assert body["opportunitiesFound"] == 11234
    assert body["generatedAt"]
    # Legacy scoring-derived metrics removed in JOB 3.
    assert "highSignalOpportunities" not in body
    assert "wtpSignals" not in body


async def test_homepage_stats_falls_back_to_opportunity_count_for_conversations(client, fake_db):
    fake_db.stub(scalar=[0, 120, 14])

    resp = await client.get("/homepage/stats")
    assert resp.status_code == 200

    body = resp.json()
    assert body["conversationsAnalyzed"] == 120
    assert body["opportunitiesFound"] == 120
