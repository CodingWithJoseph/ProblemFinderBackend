"""Tests for the /pipeline routes.

Focus: GET /pipeline/{id}/signal returns the correct shape — cluster payload
with growth fields, the cluster's posts with full signal breakdown, and the
aggregate metrics computed across those posts.
"""

import csv
import io
from datetime import datetime, timezone

import pytest

from conftest import (
    FakeResult,
    make_pipeline,
    make_cluster,
    make_cluster_member,
    make_opportunity,
    make_source_post,
)


SIGNAL_OVERVIEW = {
    "schema_version": "signal_overview.v1",
    "summary": "Freelancers struggle to get invoices paid on time.",
    "signal_metrics": {
        "posts": 2,
        "comments": 15,
        "authors": 2,
        "communities": 1,
        "agreement": 0.9,
        "focus": 0.8,
    },
    "problem_breakdown": [
        {
            "id": "late-payment",
            "title": "Late payments",
            "supporting_post_ids": ["reddit-1"],
        }
    ],
    "top_evidence": [{"source_post_id": "reddit-1", "title": "Invoice overdue"}],
    "audience_context": {"primary_audience": "Freelancers"},
    "questions_to_investigate": [{"question": "How long are invoices overdue?"}],
    "source_breakdown": [],
    "subclusters": [],
}


async def test_pipeline_signal_shape_with_cluster(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42", notes="watch this", stage="exploring")
    cluster = make_cluster(
        id=42,
        growth_rate_wow=0.35,
        growth_rate_mom=1.2,
        post_velocity=140.0,
        persistence_score=0.78,
        source_breakdown=[{"platform": "reddit", "site": "freelance", "count": 2}],
        top_tfidf_terms=[{"term": "invoice", "score": 1.2}],
        post_volume_by_date=[{"date": "2026-05-01", "count": 2}],
    )
    post1 = make_source_post(score=88, num_comments=10)
    post2 = make_source_post(score=72, num_comments=5)
    member1 = make_cluster_member(source_post_id=post1.id, similarity_score=0.95)
    member2 = make_cluster_member(source_post_id=post2.id, similarity_score=0.85)
    fake_db.stub(
        execute=[
            FakeResult(rows=[card]),            # pipeline card lookup
            FakeResult(rows=[cluster]),         # source cluster lookup
            FakeResult(rows=[(member1, post1), (member2, post2)]),  # canonical posts
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
    assert body["cluster"]["persistenceScore"] == 0.78
    assert body["analytics"]["topTerms"][0]["term"] == "invoice"

    # aggregate metrics across canonical source posts
    metrics = body["metrics"]
    assert metrics["postCount"] == 2
    assert metrics["avgSourceScore"] == 80.0
    assert metrics["commentCount"] == 15
    assert "avgSignalScore" not in metrics
    assert "wtpRate" not in metrics

    # posts with the simplified (non-scoring) shape
    assert len(body["posts"]) == 2
    post = body["posts"][0]
    assert post["sourceScore"] == 88
    for field in ["signalScore", "intensityScore", "wtpConfidence"]:
        assert field not in post


async def test_pipeline_signal_returns_populated_signal_overview(client, fake_db, auth_headers):
    generated_at = datetime(2026, 6, 8, 12, 30, tzinfo=timezone.utc)
    card = make_pipeline(source_cluster_id="42")
    cluster = make_cluster(
        signal_overview=SIGNAL_OVERVIEW,
        overview_status="ready",
        overview_error=None,
        overview_schema_version="signal_overview.v1",
        overview_generated_at=generated_at,
        overview_model_version="overview-model-v1",
        overview_input_fingerprint="sha256:abc123",
    )
    fake_db.stub(
        execute=[
            FakeResult(rows=[card]),
            FakeResult(rows=[cluster]),
            FakeResult(rows=[]),
        ]
    )

    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["signalOverview"] == SIGNAL_OVERVIEW
    assert body["overviewStatus"] == "ready"
    assert body["overviewError"] is None
    assert body["overviewSchemaVersion"] == "signal_overview.v1"
    assert body["overviewGeneratedAt"] == "2026-06-08T12:30:00+00:00"
    assert body["overviewModelVersion"] == "overview-model-v1"
    assert body["overviewInputFingerprint"] == "sha256:abc123"


async def test_pipeline_signal_returns_null_signal_overview(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    cluster = make_cluster(signal_overview=None, overview_status=None, overview_error=None)
    fake_db.stub(
        execute=[
            FakeResult(rows=[card]),
            FakeResult(rows=[cluster]),
            FakeResult(rows=[]),
        ]
    )

    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["signalOverview"] is None
    assert body["overviewStatus"] is None
    assert body["overviewError"] is None


async def test_pipeline_signal_returns_failed_overview_metadata(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    cluster = make_cluster(
        signal_overview=None,
        overview_status="failed",
        overview_error="Overview generation timed out",
    )
    fake_db.stub(
        execute=[
            FakeResult(rows=[card]),
            FakeResult(rows=[cluster]),
            FakeResult(rows=[]),
        ]
    )

    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["signalOverview"] is None
    assert body["overviewStatus"] == "failed"
    assert body["overviewError"] == "Overview generation timed out"


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


@pytest.mark.parametrize("stage", ["watching", "discovery", "exploring", "validating", "building"])
async def test_pipeline_signal_accepts_every_active_stage(stage, client, fake_db, auth_headers):
    card = make_pipeline(stage=stage)
    fake_db.stub(execute=[FakeResult(rows=[card])])
    resp = await client.get(f"/pipeline/{card.id}/signal", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["pipeline"]["stage"] == stage


async def test_pipeline_signal_rejects_removed_card(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])
    resp = await client.get(
        "/pipeline/44444444-4444-4444-4444-444444444444/signal",
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Active pipeline card not found"


async def test_pipeline_signal_evidence_cluster_posts(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    post = make_source_post(posted_at=None, scraped_at=make_pipeline().created_at)
    member = make_cluster_member(source_post_id=post.id)
    fake_db.stub(
        execute=[
            FakeResult(rows=[card]),
            FakeResult(rows=[(member, post)]),
            FakeResult(rows=[("r/freelance",)]),
        ],
        scalar=[1],
    )
    resp = await client.get(
        f"/pipeline/{card.id}/signal/evidence?sort=similarity",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["data"][0]["dateType"] == "observed"
    assert body["data"][0]["similarityScore"] == member.similarity_score
    assert body["communities"] == ["r/freelance"]


async def test_pipeline_signal_evidence_detail_checks_membership(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    post = make_source_post()
    member = make_cluster_member(source_post_id=post.id)
    fake_db.stub(execute=[FakeResult(rows=[card]), FakeResult(rows=[(member, post)])])
    resp = await client.get(
        f"/pipeline/{card.id}/signal/evidence/{post.id}",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["body"] == post.body
    assert resp.json()["topComments"] == post.top_comments


async def test_pipeline_signal_brief_unavailable_without_cluster(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id=None)
    fake_db.stub(execute=[FakeResult(rows=[card])])
    resp = await client.get(f"/pipeline/{card.id}/signal/brief", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["available"] is False


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


# ──────────────────────────────────────────────────────────────────────────
# BACKEND JOB 4: watch / unwatch
# ──────────────────────────────────────────────────────────────────────────

from app.models import PlanEnum
from conftest import make_subscription


async def test_watch_cluster_creates_card(client, fake_db, auth_headers):
    cluster = make_cluster(id=42, name="Freelance invoicing pain")
    opp = make_opportunity(cluster_id=42)
    fake_db.stub(execute=[
        FakeResult(rows=[]),                                       # _find_watch_card -> none
        FakeResult(rows=[cluster]),                               # cluster lookup
        FakeResult(rows=[]),                                      # active cards (none)
        FakeResult(rows=[make_subscription(plan=PlanEnum.pro)]),  # get_plan
        FakeResult(rows=[(opp.id,)]),                            # opportunity ids in cluster
    ])
    resp = await client.post("/pipeline/watch", json={"cluster_id": "42"}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_watched"] is True
    assert body["pipeline_id"]

    assert len(fake_db.added) == 1
    card = fake_db.added[0]
    assert card.stage == "watching"
    assert card.source_cluster_id == "42"
    assert card.name == "Freelance invoicing pain"
    assert opp.id in card.post_ids
    assert fake_db.commit_count == 1


async def test_watch_cluster_idempotent(client, fake_db, auth_headers):
    existing = make_pipeline(source_cluster_id="42")
    fake_db.stub(execute=[FakeResult(rows=[existing])])
    resp = await client.post("/pipeline/watch", json={"cluster_id": "42"}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_watched"] is True
    assert body["pipeline_id"] == existing.id
    assert fake_db.added == []  # no new card created


async def test_watch_cluster_404_when_cluster_missing(client, fake_db, auth_headers):
    fake_db.stub(execute=[
        FakeResult(rows=[]),   # no existing watch
        FakeResult(rows=[]),   # no cluster
    ])
    resp = await client.post("/pipeline/watch", json={"cluster_id": "999"}, headers=auth_headers)
    assert resp.status_code == 404


async def test_watch_cluster_free_plan_cap(client, fake_db, auth_headers):
    cluster = make_cluster(id=42)
    active = [make_pipeline(), make_pipeline(), make_pipeline()]  # at the free cap (3)
    fake_db.stub(execute=[
        FakeResult(rows=[]),                                        # no existing watch
        FakeResult(rows=[cluster]),                                # cluster lookup
        FakeResult(rows=active),                                   # active cards
        FakeResult(rows=[make_subscription(plan=PlanEnum.free)]),  # get_plan
    ])
    resp = await client.post("/pipeline/watch", json={"cluster_id": "42"}, headers=auth_headers)
    assert resp.status_code == 403
    assert resp.json()["error"] == "limit_reached"


async def test_unwatch_cluster_removes_card(client, fake_db, auth_headers):
    card = make_pipeline(source_cluster_id="42")
    fake_db.stub(execute=[FakeResult(rows=[card])])
    resp = await client.delete("/pipeline/watch/42", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["is_watched"] is False
    assert card.removed_at is not None
    assert fake_db.commit_count == 1


async def test_unwatch_cluster_noop_when_not_watched(client, fake_db, auth_headers):
    fake_db.stub(execute=[FakeResult(rows=[])])
    resp = await client.delete("/pipeline/watch/42", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["is_watched"] is False
    assert fake_db.commit_count == 0
