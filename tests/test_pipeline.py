"""Tests for the /pipeline routes.

Focus: GET /pipeline/{id}/signal returns the correct shape — cluster payload
with growth fields, the cluster's posts with full signal breakdown, and the
aggregate metrics computed across those posts.
"""

import csv
import io

from conftest import (
    FakeResult,
    make_pipeline,
    make_cluster,
    make_opportunity,
)


async def test_pipeline_signal_shape_with_cluster(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42", notes="watch this", stage="exploring")
    cluster = make_cluster(id=42, growth_rate_wow=0.35, growth_rate_mom=1.2, post_velocity=140.0)
    opp1 = make_opportunity(cluster_id=42, source_score=87.5)
    opp2 = make_opportunity(cluster_id=42, source_score=72.0)
    fake_db.stub(
        execute=[
            FakeResult(rows=[card]),            # pipeline card lookup
            FakeResult(rows=[cluster]),         # source cluster lookup
            FakeResult(rows=[opp1, opp2]),      # cluster posts
        ]
    )

    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    # pipeline anchor
    assert body["pipeline"]["id"] == card.id
    assert body["pipeline"]["notes"] == "watch this"
    assert body["pipeline"]["stage"] == "exploring"

    # cluster payload with growth fields
    assert body["cluster"]["growthRateWow"] == 0.35
    assert body["cluster"]["growthRateMom"] == 1.2
    assert body["cluster"]["postVelocity"] == 140.0

    # aggregate metrics across the two posts (scoring metrics removed in JOB 3)
    metrics = body["metrics"]
    assert metrics["postCount"] == 2
    assert metrics["avgSourceScore"] == 79.75       # (87.5 + 72.0) / 2
    assert "avgSignalScore" not in metrics
    assert "wtpRate" not in metrics

    # posts with the simplified (non-scoring) shape
    assert len(body["posts"]) == 2
    post = body["posts"][0]
    assert post["sourceScore"] == 87.5
    for field in ["signalScore", "intensityScore", "wtpConfidence"]:
        assert field not in post


async def test_pipeline_signal_falls_back_to_post_ids(client, fake_db, auth_headers):
    # No system cluster behind the card -> posts come from the card's post_ids.
    card = make_pipeline(source_cluster_id=None, post_ids=["a", "b"])
    opp1 = make_opportunity(cluster_id=None)
    opp2 = make_opportunity(cluster_id=None)
    fake_db.stub(
        execute=[
            FakeResult(rows=[card]),            # pipeline card lookup
            FakeResult(rows=[opp1, opp2]),      # posts by id
        ]
    )

    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["cluster"] is None
    assert body["metrics"]["postCount"] == 2
    assert len(body["posts"]) == 2


async def test_pipeline_signal_not_found(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])  # card lookup -> none
    resp = await client.get(
        "/pipeline/44444444-4444-4444-4444-444444444444/signal",
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_pipeline_export_csv_with_cluster_posts(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42", stage="validating")
    cluster = make_cluster(id=42, name="Freelance invoicing pain")
    opp = make_opportunity(
        cluster_id=42,
        title="CSV export should include quoted, useful data",
        source_score=91.25,
    )
    fake_db.stub(
        execute=[
            FakeResult(rows=[card]),        # pipeline card lookup
            FakeResult(rows=[cluster]),     # source cluster lookup
            FakeResult(rows=[opp]),         # cluster posts
        ]
    )

    resp = await client.get(f"/pipeline/{card.id}/export", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "problemfinder-" in resp.headers["content-disposition"]

    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["pipeline_id"] == card.id
    assert row["pipeline_stage"] == "validating"
    assert row["cluster_id"] == "42"
    assert row["post_id"] == opp.id
    assert row["title"] == "CSV export should include quoted, useful data"
    assert row["source_score"] == "91.25"
    # Scoring columns removed from the export (JOB 3).
    assert "signal_score" not in row
    assert "wtp_signal" not in row
    assert "industry_category" not in row


async def test_pipeline_export_json_reuses_signal_shape(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id=None, post_ids=["a", "b"])
    opp1 = make_opportunity(cluster_id=None, source_score=80.0)
    opp2 = make_opportunity(cluster_id=None, source_score=60.0)
    fake_db.stub(
        execute=[
            FakeResult(rows=[card]),            # pipeline card lookup
            FakeResult(rows=[opp1, opp2]),      # posts by id
        ]
    )

    resp = await client.get(f"/pipeline/{card.id}/export?format=json", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["pipeline"]["id"] == card.id
    assert body["cluster"] is None
    assert body["metrics"]["postCount"] == 2
    assert body["metrics"]["avgSourceScore"] == 70.0
    assert [post["id"] for post in body["posts"]] == [opp1.id, opp2.id]


async def test_pipeline_export_not_found(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])  # card lookup -> none
    resp = await client.get(
        "/pipeline/44444444-4444-4444-4444-444444444444/export",
        headers=auth_headers,
    )
    assert resp.status_code == 404
